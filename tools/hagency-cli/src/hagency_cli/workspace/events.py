from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["warning", "error"]


@dataclass(frozen=True)
class OperationEvent:
    message: str
    error: bool = False
    severity: Severity | None = None

    @property
    def stderr(self) -> bool:
        # The original "error" field selects the error stream, including Git progress.
        return self.error or self.severity is not None


Progress = Callable[[OperationEvent], None]


def emit_event(
    progress: Progress | None,
    message: str,
    *,
    error: bool = False,
    severity: Severity | None = None,
) -> None:
    if progress is not None:
        progress(OperationEvent(message, error or severity is not None, severity))
