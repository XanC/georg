# SPDX-License-Identifier: AGPL-3.0-only

from collections.abc import Sequence
from datetime import datetime, timedelta


class GeorgError(Exception):
    """Base class for errors raised by Georg."""


class ConfigError(GeorgError):
    """The configuration file or environment is invalid."""


class ModuleError(GeorgError):
    """A module failed. `retryable` tells the job queue whether to try again.

    `retry_after` asks the queue to wait at least that long before retrying (e.g.
    a download quota that resets hours later); such waits keep retrying past
    `max_attempts` until the queue's retry deadline.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class SourceNotReadyError(ModuleError):
    """The source recording is still being uploaded or changed during fetch."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class PublicationConflictError(ModuleError):
    """The published object was edited at the target since Georg wrote it; writing these
    fields needs a reviewer's decision, so the job is not retried."""

    def __init__(
        self,
        message: str,
        *,
        fields: Sequence[str],
        modified_by: str | None = None,
        modified_at: datetime | None = None,
    ) -> None:
        super().__init__(message, retryable=False)
        self.fields = list(fields)
        self.modified_by = modified_by
        self.modified_at = modified_at
