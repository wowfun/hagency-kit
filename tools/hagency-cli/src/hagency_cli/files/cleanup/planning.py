from __future__ import annotations

import os
import subprocess
import time
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from hagency_cli.files.cleanup import inspection
from hagency_cli.files.cleanup.models import CleanupIssue, ScanProgress, _GitContext


class PlanningSession:
    """Invocation-local planning caches, progress and shared generic scan budget."""

    def __init__(self, timeout: int, progress: Callable[[ScanProgress], None] | None):
        self.timeout = timeout
        self.remaining = float(timeout) if timeout else None
        self.progress = progress
        self.started = self.changed = time.monotonic()
        self.last_emit = float("-inf")
        self.stage = "scanning"
        self.path: Path | None = None
        self.entries = self.candidates = self.git_queries = 0
        self.stage_seconds: dict[str, float] = defaultdict(float)
        self.timeout_issues: list[CleanupIssue] = []
        self.remaining_roots: tuple[Path, ...] = ()
        self.ancestors: dict[Path, tuple[tuple[_GitContext, ...], str | None]] = {}
        self.tracked: dict[Path, tuple[tuple[bytes, ...], str | None]] = {}

    def update(self, stage: str, path: Path | None = None) -> None:
        now = time.monotonic()
        self.stage_seconds[self.stage] += now - self.changed
        self.changed = now
        self.stage, self.path = stage, path
        self.tick()

    def tick(self, *, finished: bool = False) -> None:
        now = time.monotonic()
        if self.progress is None or (not finished and now - self.last_emit < 0.2):
            return
        self.last_emit = now
        timings = dict(self.stage_seconds)
        timings[self.stage] = timings.get(self.stage, 0) + now - self.changed
        self.progress(
            ScanProgress(
                self.stage,
                self.path,
                self.entries,
                self.candidates,
                now - self.started,
                self.git_queries,
                finished,
                tuple(timings.items()),
            )
        )

    def measuring(self, path: Path, descendant: bool) -> None:
        self.entries += int(descendant)
        self.update("measuring", path)

    @contextmanager
    def budget(self) -> Iterator[None]:
        started = time.monotonic()
        try:
            with inspection.scan_budget(self.remaining, limit=self.timeout):
                yield
        finally:
            if self.remaining is not None:
                self.remaining = max(0, self.remaining - (time.monotonic() - started))

    def timed_out(self, error: inspection.ScanTimeout, path: Path | None) -> None:
        if not self.timeout_issues:
            self.timeout_issues.append(
                CleanupIssue(
                    "scan_timeout",
                    path,
                    f"{error}; scan incomplete at {self.path or path} ({self.stage}); "
                    f"incomplete roots: {', '.join(map(str, self.remaining_roots)) or 'candidate finalization'}. "
                    "Only fully checked candidates are available.",
                )
            )

    def git_contexts(self, path: Path) -> tuple[tuple[_GitContext, ...], str | None]:
        is_directory = path.is_dir()
        origin = path if is_directory else path.parent
        current = origin
        pending = []
        while current not in self.ancestors:
            inspection.checkpoint()
            context, error = inspection._git_context_from_marker(current / ".git")
            pending.append((current, context, error))
            if error or current.parent == current:
                break
            current = current.parent
        contexts, error = self.ancestors.get(current, ((), None))
        for directory, context, local_error in reversed(pending):
            error = local_error or error
            if context is not None:
                contexts = tuple(
                    sorted(
                        (*contexts, context),
                        key=lambda item: inspection._canonical_key(item.root),
                    )
                )
            self.ancestors[directory] = (contexts, error)
        contexts, error = self.ancestors[origin]
        if is_directory and error is None:
            # Ancestor discovery is shared; every directory still gets a complete
            # nested-repository inspection, including newly encountered markers.
            return inspection._discover_nested_git_contexts(path, contexts)
        return contexts, error

    def git_tracked(self, path: Path, root: Path) -> tuple[bool | None, str | None]:
        if root not in self.tracked:
            inspection.checkpoint()
            self.git_queries += 1
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment["GIT_OPTIONAL_LOCKS"] = "0"
            try:
                result = subprocess.run(
                    [
                        "git",
                        "--literal-pathspecs",
                        "-C",
                        str(root),
                        "ls-files",
                        "--cached",
                        "--full-name",
                        "-z",
                    ],
                    capture_output=True,
                    check=False,
                    env=environment,
                    timeout=inspection.git_query_timeout(),
                )
                inspection.checkpoint()
                if result.returncode:
                    self.tracked[root] = (
                        (),
                        result.stderr.decode(errors="replace").strip()
                        or f"git exited with {result.returncode}",
                    )
                else:
                    # One byte string per index record, without materializing all
                    # parent directories. Prefix queries use the sorted file list.
                    tracked = tuple(
                        sorted(raw for raw in result.stdout.split(b"\0") if raw)
                    )
                    inspection.checkpoint()
                    self.tracked[root] = (tracked, None)
            except inspection.ScanTimeout:
                raise
            except (OSError, subprocess.TimeoutExpired) as exc:
                inspection.checkpoint()
                self.tracked[root] = ((), str(exc))
        tracked, error = self.tracked[root]
        if error:
            return None, error
        relative = path.relative_to(root).as_posix()
        if relative == ".":
            return bool(tracked), None
        name = os.fsencode(relative)
        index = bisect_left(tracked, name)
        if index < len(tracked) and tracked[index] == name:
            return True, None
        prefix = name + b"/"
        index = bisect_left(tracked, prefix)
        return index < len(tracked) and tracked[index].startswith(prefix), None


_SESSION: ContextVar[PlanningSession | None] = ContextVar(
    "cleanup_planning", default=None
)


def current_session() -> PlanningSession | None:
    return _SESSION.get()


@contextmanager
def planning_session(
    timeout: int, progress: Callable[[ScanProgress], None] | None
) -> Iterator[PlanningSession]:
    session = PlanningSession(timeout, progress)
    token = _SESSION.set(session)
    hook = inspection._CHECKPOINT_HOOK.set(session.tick)
    measure_hook = inspection._MEASURE_HOOK.set(session.measuring)
    try:
        yield session
    finally:
        inspection._MEASURE_HOOK.reset(measure_hook)
        inspection._CHECKPOINT_HOOK.reset(hook)
        _SESSION.reset(token)
        session.tick(finished=True)
