# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_drive import FakeDrive
from sqlalchemy import Engine

from georg.db import create_engine, upgrade


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "georg.sqlite3"
    upgrade(path)
    return path


@pytest.fixture
def engine(db_path: Path) -> Iterator[Engine]:
    engine = create_engine(db_path)
    yield engine
    engine.dispose()


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()
