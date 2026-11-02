# SPDX-License-Identifier: AGPL-3.0-only
import stat
from pathlib import Path
from typing import Any

import pytest
from deploy_scripts import load

boot = load("boot/georg_boot.py", "georg_boot")

SECRET = "s3cr3t-value"


class FakeSsm:
    def __init__(self, params: dict[str, str], page_size: int = 2) -> None:
        self.params = params
        self.page_size = page_size
        self.calls: list[dict[str, Any]] = []

    def get_parameters_by_path(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        assert kwargs["WithDecryption"] is True and kwargs["Recursive"] is True
        names = sorted(n for n in self.params if n.startswith(kwargs["Path"]))
        start = int(kwargs.get("NextToken", 0))
        page = names[start : start + self.page_size]
        result: dict[str, Any] = {
            "Parameters": [{"Name": n, "Value": self.params[n]} for n in page]
        }
        if start + self.page_size < len(names):
            result["NextToken"] = str(start + self.page_size)
        return result


class FakeRoute53:
    def __init__(self) -> None:
        self.changes: list[dict[str, Any]] = []

    def change_resource_record_sets(self, **kwargs: Any) -> dict[str, Any]:
        self.changes.append(kwargs)
        return {}


PARAMS = {
    "/georg/GEORG_SECRET_KEY": SECRET,
    "/georg/GEORG_WP_KEY": "wp-key",
    "/georg/GEORG_SMTP_PASSWORD": "a+b/c=$HOME",
    "/georg/files/google-service-account.json": '{\n  "type": "service_account"\n}\n',
    "/other/IGNORED": "x",
}


def _run(
    tmp_path: Path, environ: dict[str, str], params: dict[str, str] = PARAMS
) -> tuple[FakeRoute53, list[str]]:
    ssm, route53 = FakeSsm(params), FakeRoute53()
    clients = {"ssm": ssm, "route53": route53}
    logs: list[str] = []
    boot.boot(
        environ,
        client=clients.__getitem__,
        imds=lambda path: "203.0.113.7\n" if path == "meta-data/public-ipv4" else "",
        run_dir=tmp_path / "run",
        log=logs.append,
    )
    return route53, logs


def test_writes_env_and_secret_files_privately(tmp_path: Path) -> None:
    _, logs = _run(tmp_path, {"GEORG_BACKUP_BUCKET": "bucket"})
    run = tmp_path / "run"
    env_file = run / "georg.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text() == (
        "GEORG_SECRET_KEY='s3cr3t-value'\nGEORG_SMTP_PASSWORD='a+b/c=$HOME'\nGEORG_WP_KEY='wp-key'\n"
    )
    sa = run / "secrets" / "google-service-account.json"
    assert stat.S_IMODE(sa.stat().st_mode) == 0o600
    assert sa.read_text().startswith("{\n")
    assert stat.S_IMODE((run / "secrets").stat().st_mode) == 0o700
    assert (run / "shutdown").is_dir()
    host = (run / "host.env").read_text()
    assert "GEORG_BACKUP_BUCKET='bucket'" in host
    assert "COMPOSE_PROFILES='backup'" in host
    assert SECRET not in host
    assert not any(SECRET in line for line in logs)


def test_stale_secret_files_are_removed(tmp_path: Path) -> None:
    _run(tmp_path, {})
    _run(tmp_path, {}, {"/georg/GEORG_SECRET_KEY": SECRET})
    assert list((tmp_path / "run" / "secrets").iterdir()) == []


def test_updates_dns_when_zone_configured(tmp_path: Path) -> None:
    route53, _ = _run(
        tmp_path,
        {
            "GEORG_HOSTED_ZONE_ID": "Z123",
            "GEORG_ORIGIN_HOSTNAME": "origin.georg.example.org",
            "GEORG_HOSTNAME": "georg.example.org",
        },
    )
    (change,) = route53.changes
    assert change["HostedZoneId"] == "Z123"
    record = change["ChangeBatch"]["Changes"][0]
    assert record["Action"] == "UPSERT"
    assert record["ResourceRecordSet"] == {
        "Name": "origin.georg.example.org",
        "Type": "A",
        "TTL": 60,
        "ResourceRecords": [{"Value": "203.0.113.7"}],
    }
    host = (tmp_path / "run" / "host.env").read_text()
    assert "GEORG_ORIGIN_HOSTNAME='origin.georg.example.org'" in host


def test_dns_falls_back_to_hostname_without_origin_hostname(tmp_path: Path) -> None:
    route53, _ = _run(
        tmp_path, {"GEORG_HOSTED_ZONE_ID": "Z123", "GEORG_HOSTNAME": "georg.example.org"}
    )
    (change,) = route53.changes
    assert change["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]["Name"] == ("georg.example.org")


def test_dns_is_optional(tmp_path: Path) -> None:
    route53, logs = _run(tmp_path, {})
    assert route53.changes == []
    assert any("skipping" in line for line in logs)
    assert "COMPOSE_PROFILES" not in (tmp_path / "run" / "host.env").read_text()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("/georg/lower_case", "x"),
        ("/georg/nested/NAME", "x"),
        ("/georg/MULTI", "a\nb"),
        ("/georg/QUOTE", "it's"),
        ("/georg/files/../escape", "x"),
    ],
)
def test_rejects_bad_parameters_without_leaking_values(
    tmp_path: Path, name: str, value: str
) -> None:
    with pytest.raises(boot.BootError) as info:
        _run(tmp_path, {}, {name: value + SECRET})
    assert SECRET not in str(info.value)


def test_pagination(tmp_path: Path) -> None:
    params = {f"/georg/VAR_{i}": str(i) for i in range(5)}
    ssm = FakeSsm(params, page_size=2)
    assert boot.fetch_parameters(ssm, "/georg/") == {f"VAR_{i}": str(i) for i in range(5)}
    assert len(ssm.calls) == 3
