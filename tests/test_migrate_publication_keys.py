# SPDX-License-Identifier: AGPL-3.0-only
"""`georg migrate-publication-keys`: the one-time move to church-UUID keys."""

import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine

from georg.cli import WP_CLI, main
from georg.core.types import (
    ExternalRef,
    SermonMetadata,
    ServiceBoundaries,
    SourceItem,
    SourcePart,
)
from georg.db import upgrade
from georg.db.engine import create_engine, database_path, session_factory
from georg.db.repositories import (
    IdentityRepository,
    PublicationRepository,
    SermonRepository,
    ServiceRepository,
)

ITEM = SourceItem(
    source_type="upload",
    source_ref="rec-1",
    parts=(SourcePart(ref="a", name="a.mp4", size=1, checksum="aa"),),
    service_date=date(2026, 11, 1),
)
BOUNDS = ServiceBoundaries(service_start_ms=0, sermon_start_ms=1, sermon_end_ms=2, service_end_ms=3)
META = SermonMetadata(title="Saints")


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "georg.toml"
    processors = "\n".join(
        f'[processors.{k}]\ntype = "unused"'
        for k in ("classifier", "detector", "transcriber", "formatter", "metadata")
    )
    path.write_text(
        f'instance = "stpaul"\ndata_dir = "{tmp_path / "data"}"\n'
        '[[inputs]]\nname = "drive"\ntype = "local_folder"\n'
        f'[inputs.options]\nroot = "{tmp_path}"\n\n{processors}\n'
        '[[outputs]]\nname = "website"\ntype = "wordpress"\n'
        '[[outputs]]\nname = "archive"\ntype = "google_drive_upload"\n'
        '[[outputs]]\nname = "files"\ntype = "sftp"\n'
    )
    return path


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[Path, Engine, int]:
    """A migrated database with three publications on the old key form."""
    config = _config_file(tmp_path)
    path = database_path(tmp_path / "data")
    upgrade(path)
    engine = create_engine(path)
    sessions = session_factory(engine)
    with sessions.begin() as s:
        service, _ = ServiceRepository(s).add_discovered(ITEM)
        sermon = SermonRepository(s).create(service.id)
        revision = SermonRepository(s).add_revision(
            sermon.id, author="system", boundaries=BOUNDS, metadata=META
        )
        pubs = PublicationRepository(s)
        pubs.prepare(sermon.id, "website", revision.id, "georg:stpaul:sermon-1:website")
        pubs.prepare(sermon.id, "archive", revision.id, "georg:stpaul:sermon-1:archive")
        pubs.prepare(sermon.id, "files", revision.id, "georg:stpaul:sermon-1:files")
    return config, engine, sermon.id


def _keys(engine: Engine, sermon_id: int) -> dict[str, str]:
    sessions = session_factory(engine)
    with sessions() as s:
        pubs = PublicationRepository(s).for_sermon(sermon_id)
        return {p.target_name: p.publication_key for p in pubs}


class _FakeDrive:
    def __init__(self) -> None:
        self.rekeys: list[tuple[str, str, str | None]] = []
        self.found: set[str] = set()

    def rekey(self, old_key: str, new_key: str, *, file_id: str | None = None) -> str | None:
        self.rekeys.append((old_key, new_key, file_id))
        self.found.add(new_key)
        return file_id or "drive-file-1"

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        if publication_key not in self.found:
            return None
        return ExternalRef(target="archive", external_id="drive-file-1", url=None)


def test_identity_is_generated_once(engine: Engine) -> None:
    sessions = session_factory(engine)
    with sessions.begin() as s:
        church_id = IdentityRepository(s).church_id()
    uuid.UUID(church_id)
    with sessions() as s:
        assert IdentityRepository(s).church_id() == church_id


def test_dry_run_lists_and_writes_wp_script(
    seeded: tuple[Path, Engine, int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, engine, sermon_id = seeded
    script = tmp_path / "rekey.sh"

    assert (
        main(
            [
                "migrate-publication-keys",
                "--old-instance",
                "stpaul",
                "--config",
                str(config),
                "--wp-script",
                str(script),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "3 to migrate" in out
    assert "dry run" in out

    text = script.read_text()
    church_id = next(
        line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("church_id: ")
    )
    uuid.UUID(church_id)
    assert WP_CLI in text
    assert "--meta_value='georg:stpaul:sermon-1:website'" in text
    assert f"_georg_publication_key 'georg:{church_id}:sermon-1:website'" in text
    # Only the WordPress target's keys go in the script; Drive and SFTP do not.
    assert "sermon-1:archive" not in text and "sermon-1:files" not in text

    assert all(k.startswith("georg:stpaul:") for k in _keys(engine, sermon_id).values())
    engine.dispose()


def test_apply_updates_drive_and_database(
    seeded: tuple[Path, Engine, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, engine, sermon_id = seeded
    drive = _FakeDrive()
    monkeypatch.setattr("georg.pipeline.registry.build_output", lambda *a, **kw: drive)

    argv = ["migrate-publication-keys", "--old-instance", "stpaul", "--config", str(config)]
    assert main([*argv, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "updated 3 publication row(s)" in out
    [rekeyed] = drive.rekeys
    assert rekeyed[0] == "georg:stpaul:sermon-1:archive"

    keys = _keys(engine, sermon_id)
    church_id = keys["website"].split(":")[1]
    uuid.UUID(church_id)
    assert keys == {
        "website": f"georg:{church_id}:sermon-1:website",
        "archive": f"georg:{church_id}:sermon-1:archive",
        "files": f"georg:{church_id}:sermon-1:files",
    }

    # A second run is a no-op.
    drive.rekeys.clear()
    assert main([*argv, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "3 publication(s) already on the new key" in out
    assert "updated 0 publication row(s)" in out
    assert drive.rekeys == []
    engine.dispose()


def test_verify_checks_each_target(
    seeded: tuple[Path, Engine, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, engine, _ = seeded
    drive = _FakeDrive()
    monkeypatch.setattr("georg.pipeline.registry.build_output", lambda *a, **kw: drive)
    argv = ["migrate-publication-keys", "--old-instance", "stpaul", "--config", str(config)]
    assert main([*argv, "--apply"]) == 0
    capsys.readouterr()

    # The fake output stands in for both wordpress and google_drive_upload targets;
    # only the Drive file actually got re-keyed, so the website key reports missing.
    assert main([*argv, "--verify"]) == 1
    out = capsys.readouterr().out
    assert "NOT FOUND" in out
    assert "not externally verifiable" in out  # the sftp target stores no key

    drive.found.add(next(iter(drive.found)).replace("archive", "website"))
    assert main([*argv, "--verify"]) == 0
    engine.dispose()
