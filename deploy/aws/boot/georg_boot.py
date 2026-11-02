#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare the Georg EC2 instance at every boot (run as root by georg-boot.service).

1. Read the SSM parameters under $GEORG_SSM_PREFIX (default /georg/):
   /georg/NAME          -> NAME='value' in /run/georg/georg.env (mode 0600)
   /georg/files/NAME    -> /run/georg/secrets/NAME (mode 0600, owned by Georg's uid)
2. Write /run/georg/host.env, the non-secret variables for docker compose.
3. If $GEORG_HOSTED_ZONE_ID is set, point the A record for $GEORG_ORIGIN_HOSTNAME
   at this instance's public IPv4 address (no Elastic IP is billed while it is
   stopped; the stale record while stopped just fails CloudFront's origin TLS
   check and fails over to the wake page).
4. Create /run/georg/shutdown, where the idle watchdog asks the host to power off.

/run is a tmpfs, so secrets never reach the data volume or its snapshots. Only
parameter names are logged, never values. Uses boto3 (Ubuntu: python3-boto3).
"""

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

GEORG_UID = 1000
FILES = "files/"
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IMDS = "http://169.254.169.254/latest"


class BootError(Exception):
    pass


def fetch_parameters(ssm: Any, prefix: str) -> dict[str, str]:
    """All parameters under `prefix`, decrypted, keyed by name relative to `prefix`."""
    params: dict[str, str] = {}
    kwargs: dict[str, Any] = {"Path": prefix, "Recursive": True, "WithDecryption": True}
    while True:
        page = ssm.get_parameters_by_path(**kwargs)
        for p in page["Parameters"]:
            params[p["Name"].removeprefix(prefix)] = p["Value"]
        if not page.get("NextToken"):
            return params
        kwargs["NextToken"] = page["NextToken"]


def split_parameters(params: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split into environment variables and secret files, validating names and values."""
    env: dict[str, str] = {}
    files: dict[str, str] = {}
    for name, value in params.items():
        if name.startswith(FILES):
            file_name = name.removeprefix(FILES)
            if not _FILE_NAME.match(file_name):
                raise BootError(f"parameter {FILES}{file_name}: invalid file name")
            files[file_name] = value
        elif _ENV_NAME.match(name):
            if "\n" in value or "'" in value:
                raise BootError(
                    f"parameter {name}: newlines and single quotes are not allowed in "
                    f"environment values; store it under {FILES} instead"
                )
            env[name] = value
        else:
            raise BootError(f"parameter {name}: expected NAME (A-Z, 0-9, _) or {FILES}NAME")
    return env, files


def format_env(env: Mapping[str, str]) -> str:
    """Single-quoted values, which docker compose takes literally."""
    return "".join(f"{k}='{v}'\n" for k, v in sorted(env.items()))


def write_file(path: Path, data: str, *, mode: int, uid: int = 0, gid: int = 0) -> None:
    """Write atomically, with `mode` and owner set before any data is written."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        os.write(fd, data.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def make_dir(path: Path, *, mode: int, uid: int = 0, gid: int = 0) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    if os.geteuid() == 0:
        os.chown(path, uid, gid)


def imds_get(path: str) -> str:
    """Read instance metadata with IMDSv2."""
    token_req = urllib.request.Request(
        f"{IMDS}/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"},
    )
    with urllib.request.urlopen(token_req, timeout=5) as r:
        token = r.read().decode()
    req = urllib.request.Request(f"{IMDS}/{path}", headers={"X-aws-ec2-metadata-token": token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return str(r.read().decode())


def update_dns(route53: Any, zone_id: str, hostname: str, ip: str) -> None:
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Comment": "Georg boot",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": hostname,
                        "Type": "A",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": ip}],
                    },
                }
            ],
        },
    )


def boot(
    environ: Mapping[str, str],
    *,
    client: Callable[[str], Any],
    imds: Callable[[str], str] = imds_get,
    run_dir: Path = Path("/run/georg"),
    log: Callable[[str], None] = print,
) -> None:
    region = environ.get("GEORG_REGION", "us-east-2")
    origin_hostname = environ.get("GEORG_ORIGIN_HOSTNAME") or environ.get(
        "GEORG_HOSTNAME", "georg.stpaulaustin.org"
    )
    hostname = environ.get("GEORG_HOSTNAME", "georg.stpaulaustin.org")
    prefix = environ.get("GEORG_SSM_PREFIX", "/georg/")
    if not prefix.endswith("/"):
        prefix += "/"
    bucket = environ.get("GEORG_BACKUP_BUCKET", "")
    zone_id = environ.get("GEORG_HOSTED_ZONE_ID", "")

    env, files = split_parameters(fetch_parameters(client("ssm"), prefix))
    make_dir(run_dir, mode=0o755)
    write_file(run_dir / "georg.env", format_env(env), mode=0o600)
    secrets = run_dir / "secrets"
    make_dir(secrets, mode=0o700, uid=GEORG_UID, gid=GEORG_UID)
    for old in secrets.iterdir():
        if old.name not in files:
            old.unlink()
    for name, value in files.items():
        write_file(secrets / name, value, mode=0o600, uid=GEORG_UID, gid=GEORG_UID)
    log(f"secrets: {len(env)} variable(s) {sorted(env)}, {len(files)} file(s) {sorted(files)}")

    make_dir(run_dir / "shutdown", mode=0o700, uid=GEORG_UID, gid=GEORG_UID)
    host = {
        "GEORG_HOSTNAME": hostname,
        "GEORG_ORIGIN_HOSTNAME": origin_hostname,
        "GEORG_BACKUP_BUCKET": bucket,
        "AWS_REGION": region,
        "GEORG_ROOT": environ.get("GEORG_ROOT", "/srv/georg"),
        "GEORG_RUN": str(run_dir),
    }
    if bucket:
        host["COMPOSE_PROFILES"] = "backup"
    write_file(run_dir / "host.env", format_env(host), mode=0o644)

    if zone_id:
        ip = imds("meta-data/public-ipv4").strip()
        if not ip:
            raise BootError("instance has no public IPv4 address")
        update_dns(client("route53"), zone_id, origin_hostname, ip)
        log(f"dns: {origin_hostname} A {ip}")
    else:
        log("dns: GEORG_HOSTED_ZONE_ID not set; skipping")


def main() -> int:
    import boto3  # type: ignore[import-not-found,unused-ignore]

    region = os.environ.get("GEORG_REGION", "us-east-2")
    try:
        boot(os.environ, client=lambda name: boto3.client(name, region_name=region))
    except BootError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
