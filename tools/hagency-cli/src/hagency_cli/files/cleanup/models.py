from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

MIN_AGE_SECONDS = 7 * 24 * 60 * 60


MAX_SCAN_DEPTH = 6


CACHEDIR_TAG_NAME = "CACHEDIR.TAG"


CACHEDIR_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


MONOREPO_INDICATORS = (
    ".git",
    "lerna.json",
    "pnpm-workspace.yaml",
    "nx.json",
    "rush.json",
)


PROJECT_INDICATORS = (
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "build.gradle",
    "terragrunt.hcl",
    "Gemfile",
    "composer.json",
    "pubspec.yaml",
    "Package.swift",
    "Makefile",
    "build.zig",
    "build.zig.zon",
    ".git",
)


DEFAULT_ROOT_NAMES = (
    "www",
    "dev",
    "Projects",
    "GitHub",
    "Code",
    "Workspace",
    "Repos",
    "Development",
)


EXPLICIT_HIDDEN_ROOTS = (
    Path(".codex") / "worktrees",
    Path(".claude") / "worktrees",
)


EXCLUDED_HOME_CHILDREN = frozenset(
    {
        "Applications",
        "AppData",
        "Box",
        "Desktop",
        "Documents",
        "Downloads",
        "Library",
        "Movies",
        "Music",
        "Pictures",
        "Public",
        "Dropbox",
        "Google Drive",
        "iCloud Drive",
    }
)


CLOUD_HOME_CHILD_PREFIXES = (
    "box",
    "creative cloud files",
    "dropbox",
    "google drive",
    "icloud",
    "nextcloud",
    "onedrive",
    "owncloud",
)


SCAN_PRUNE_NAMES = frozenset(
    {".git", ".hg", ".svn", ".Trash", "Applications", "AppData", "Library"}
)


SCAN_PRUNE_NAMES_CASEFOLD = frozenset(name.casefold() for name in SCAN_PRUNE_NAMES)


def _unsafe_text(value: str) -> bool:
    return any(
        ord(c) < 32
        or ord(c) == 127
        or c
        in "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for c in value
    )


def _display_text(value: object) -> str:
    return "".join(f"\\u{ord(c):04x}" if _unsafe_text(c) else c for c in str(value))


class Activity(str, Enum):
    OLD = "old"
    RECENT = "recent"
    UNCERTAIN = "uncertain"


class CleanupDisposition(str, Enum):
    PREVIEW = "preview"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    PARTIAL = "partial"


class ItemDisposition(str, Enum):
    WOULD_REMOVE = "would_remove"
    REMOVED = "removed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class PurgeRequest:
    paths: tuple[Path, ...] = ()
    dry_run: bool = False
    recursive: bool = False
    config: Path | None = None
    include_unverified: bool = False


@dataclass(frozen=True)
class CleanRequest:
    agents: tuple[str, ...] = ()
    dry_run: bool = False
    config: Path | None = None


@dataclass(frozen=True)
class CleanupRule:
    id: str
    command: str
    source: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    enabled: bool = True
    root: Path | None = None
    min_age_days: int = 0
    preselect_after_days: int = 0
    keep_previous: int = 1
    origin: str = "builtin"

    @property
    def agent(self) -> str | None:
        return self.source.split(".")[0] if "." in self.source else None


@dataclass(frozen=True)
class RuleScope:
    rule: CleanupRule
    root: Path


@dataclass(frozen=True)
class RulesSnapshot:
    rules: tuple[CleanupRule, ...]
    files: tuple[tuple[Path, str | None], ...]


@dataclass(frozen=True)
class CleanupCandidate:
    id: str
    exact_path: Path
    project_path: Path
    artifact_kind: str
    size_bytes: int | None
    activity: Activity
    preselected: bool
    rule_ids: tuple[str, ...] = ()
    rule_origins: tuple[str, ...] = ()
    entry_kind: str = "directory"
    group: str = ""
    project_verified: bool = True
    artifact_unverified_reason: str | None = None

    @property
    def verification_note(self) -> str:
        notes = []
        if not self.project_verified:
            notes.append("project context unverified")
        if self.artifact_unverified_reason:
            notes.append(
                f"artifact purpose unverified: {self.artifact_unverified_reason}"
            )
        return " | ".join(notes)

    @property
    def label(self) -> str:
        size = (
            _format_bytes(self.size_bytes) if self.size_bytes is not None else "unknown"
        )
        return (
            f"{self.project_path.name} | {self.artifact_kind} | {self.entry_kind} | {size} | "
            f"{self.activity.value} | {self.exact_path} | "
            f"rules: {', '.join(self.rule_ids)} | config: {', '.join(self.rule_origins)}"
            + (f" | {self.verification_note}" if self.verification_note else "")
        )


class CleanupUI(Protocol):
    def is_interactive(self) -> bool: ...

    def select(
        self, choices: tuple[CleanupCandidate, ...]
    ) -> tuple[str, ...] | None: ...

    def confirm_exact(self, paths: tuple[Path, ...], known_bytes: int) -> bool: ...

    def present_plan(self, plan: CleanupPlan) -> None: ...


@dataclass(frozen=True)
class CleanupIssue:
    code: str
    path: Path | None
    message: str
    is_failure: bool = True


@dataclass(frozen=True)
class CleanupItemResult:
    exact_path: Path
    disposition: ItemDisposition
    size_bytes: int | None
    message: str = ""


@dataclass(frozen=True)
class CleanupReport:
    disposition: CleanupDisposition
    roots: tuple[Path, ...]
    choices: tuple[CleanupCandidate, ...]
    selected_paths: tuple[Path, ...]
    results: tuple[CleanupItemResult, ...]
    issues: tuple[CleanupIssue, ...]
    known_bytes: int = 0
    command: str = "purge"
    config_files: tuple[Path, ...] = ()

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues) or any(
            result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
            for result in self.results
        )

    @property
    def exit_code(self) -> int:
        return (
            0 if self.disposition is CleanupDisposition.CANCELLED else int(self.failed)
        )


@dataclass(frozen=True)
class PathsEditReport:
    config_path: Path
    before_roots: tuple[Path, ...]
    after_roots: tuple[Path, ...]
    editor: str
    issues: tuple[CleanupIssue, ...]

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _GitContext:
    root: Path
    marker_identity: _Identity


@dataclass(frozen=True)
class _HardlinkEntry:
    identity: tuple[int, int] | None
    size_bytes: int


@dataclass(frozen=True)
class _TreeEntry:
    relative_path: str
    identity: _Identity
    size: int
    mtime_ns: int
    ctime_ns: int
    owner_uid: int | None = None


@dataclass(frozen=True)
class _BoundCandidate:
    choice: CleanupCandidate
    root: Path
    root_identity: _Identity
    parent_identity: _Identity
    target_identity: _Identity
    git_contexts: tuple[_GitContext, ...]
    hardlink_entries: tuple[_HardlinkEntry, ...]
    scopes: tuple[RuleScope, ...] = ()
    exclusions: tuple[RuleScope, ...] = ()
    agent: str | None = None
    tree: tuple[_TreeEntry, ...] = ()
    removed_links: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True)
class CleanupPlan:
    roots: tuple[Path, ...]
    candidates: tuple[_BoundCandidate, ...]
    issues: tuple[CleanupIssue, ...]
    snapshot: RulesSnapshot | None = None
    command: str = "purge"


@dataclass(frozen=True)
class _ConfiguredPaths:
    paths: tuple[Path, ...]
    has_entries: bool
    issues: tuple[CleanupIssue, ...]


@dataclass(frozen=True)
class _DiscoveredRoots:
    paths: tuple[Path, ...]
    issues: tuple[CleanupIssue, ...]


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
