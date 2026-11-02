# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

import pytest
from deploy_scripts import load

wake = load("wake/index.py", "georg_wake")


class FakeEc2:
    def __init__(self, state: str) -> None:
        self.state = state
        self.started: list[list[str]] = []

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        assert InstanceIds == ["i-0123"]
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}

    def start_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        self.started.append(InstanceIds)
        return {}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTANCE_ID", "i-0123")
    monkeypatch.setenv("APP_URL", "https://georg.example.org/")


def _call(ec2: FakeEc2, *, healthy: bool = False, **event: str) -> dict[str, Any]:
    probes: list[str] = []

    def probe(url: str) -> bool:
        probes.append(url)
        return healthy

    result: dict[str, Any] = wake.handler(event, None, ec2=ec2, probe=probe)
    if probes:
        assert probes == ["https://georg.example.org"]
    return result


def test_stopped_instance_is_started() -> None:
    ec2 = FakeEc2("stopped")
    result = _call(ec2, rawPath="/sermons/5")
    assert ec2.started == [["i-0123"]]
    assert result["statusCode"] == 200
    assert "about a minute" in result["body"]
    assert 'http-equiv="refresh"' in result["body"]
    assert result["headers"]["Cache-Control"] == "no-store"


def test_starting_instance_shows_page_without_starting_again() -> None:
    for state, healthy in (("pending", False), ("running", False)):
        ec2 = FakeEc2(state)
        result = _call(ec2, healthy=healthy)
        assert result["statusCode"] == 200
        assert "about a minute" in result["body"]
        assert ec2.started == []


def test_healthy_instance_redirects_keeping_path() -> None:
    result = _call(FakeEc2("running"), healthy=True, rawPath="/sermons/5", rawQueryString="a=1")
    assert result["statusCode"] == 302
    assert result["headers"]["Location"] == "https://georg.example.org/sermons/5?a=1"


def test_redirect_cannot_leave_the_app_host() -> None:
    result = _call(FakeEc2("running"), healthy=True, rawPath="//evil.example/x")
    assert result["headers"]["Location"] == "https://georg.example.org/evil.example/x"


def test_stopping_instance_waits() -> None:
    ec2 = FakeEc2("stopping")
    result = _call(ec2)
    assert "shutting down" in result["body"]
    assert ec2.started == []


def test_terminated_instance_is_an_error() -> None:
    result = _call(FakeEc2("terminated"))
    assert result["statusCode"] == 503


def test_healthy_probe_handles_connection_errors() -> None:
    assert wake.healthy("http://127.0.0.1:9", timeout=0.5) is False
