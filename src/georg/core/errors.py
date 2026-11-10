# SPDX-License-Identifier: AGPL-3.0-only

from datetime import timedelta


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
