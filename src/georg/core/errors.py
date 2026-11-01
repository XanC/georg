# SPDX-License-Identifier: AGPL-3.0-only


class GeorgError(Exception):
    """Base class for errors raised by Georg."""


class ConfigError(GeorgError):
    """The configuration file or environment is invalid."""


class ModuleError(GeorgError):
    """A module failed. `retryable` tells the job queue whether to try again."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SourceNotReadyError(ModuleError):
    """The source recording is still being uploaded or changed during fetch."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)
