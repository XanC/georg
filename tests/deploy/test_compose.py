# SPDX-License-Identifier: AGPL-3.0-only
import re
from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
SECRET_NAMES = re.compile(r"(SECRET|PASSWORD|_KEY|TOKEN)", re.IGNORECASE)


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load((DEPLOY / "compose.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_services() -> None:
    services = _compose()["services"]
    assert {"georg-web", "georg-worker", "caddy", "litestream"} <= set(services)
    assert services["caddy"]["ports"][:2] == ["80:80", "443:443"]
    assert "${GEORG_ROOT:-/srv/georg}/caddy/data:/data" in services["caddy"]["volumes"]


def test_secrets_only_from_env_file() -> None:
    compose = _compose()
    for name, service in compose["services"].items():
        for key in service.get("environment", {}):
            assert not SECRET_NAMES.search(key), f"{name}: {key} must come from the env file"
    for name in ("georg-web", "georg-worker"):
        assert compose["services"][name]["env_file"] == ["${GEORG_RUN:-/run/georg}/georg.env"]


def test_worker_can_request_poweroff() -> None:
    worker = _compose()["services"]["georg-worker"]
    assert "${GEORG_RUN:-/run/georg}/shutdown:/run/georg/shutdown" in worker["volumes"]
    unit = (DEPLOY / "aws/systemd/georg-poweroff.path").read_text()
    assert "PathExists=/run/georg/shutdown/poweroff" in unit


def test_litestream_replicates_the_database() -> None:
    config = yaml.safe_load((DEPLOY / "litestream.yml").read_text())
    (db,) = config["dbs"]
    assert db["path"] == "/var/lib/georg/georg.sqlite3"
    assert db["replicas"][0]["type"] == "s3"
    assert db["replicas"][0]["bucket"] == "${GEORG_BACKUP_BUCKET}"


def test_caddy_serves_only_the_origin_hostname() -> None:
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    assert "{$GEORG_ORIGIN_HOSTNAME:georg.stpaulaustin.org}" in caddyfile
    assert "GEORG_HOSTNAME" not in caddyfile
    env = _compose()["services"]["caddy"]["environment"]
    assert "GEORG_ORIGIN_HOSTNAME" in env


def test_aws_config_example_loads_and_watchdog_reaches_the_host() -> None:
    from georg.core.config import load_config

    config = load_config(DEPLOY / "georg.aws.toml")
    assert config.idle_shutdown_minutes is not None
    assert config.idle_shutdown_command == ("touch", "/run/georg/shutdown/poweroff")
    data_mount = "${GEORG_ROOT:-/srv/georg}/data:" + str(config.data_dir)
    assert data_mount in _compose()["services"]["georg-worker"]["volumes"]


def test_aws_config_sftp_output_options_validate() -> None:
    from georg.core.config import load_config
    from georg.modules.outputs.sftp import SftpOptions

    config = load_config(DEPLOY / "georg.aws.toml")
    (phone,) = [o for o in config.outputs if o.type == "sftp"]
    options = SftpOptions.model_validate(dict(phone.options))
    assert str(options.key_path) == "/run/secrets/sftp-key"
    assert str(options.known_hosts) == "/run/secrets/sftp-known-hosts"
