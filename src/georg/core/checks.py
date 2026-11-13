# SPDX-License-Identifier: AGPL-3.0-only
"""Optional connection checks for configured modules, run from the admin settings page.

Not part of `georg.core.contracts`: a module opts in by having a `check()` method.
A check is read-only. It never uploads, writes or sends anything, and its message
never contains secrets.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


@runtime_checkable
class ConnectionCheck(Protocol):
    def check(self) -> CheckResult: ...
