from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from hagency_cli.files.cleanup.conditions import Condition, entries

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


CLEANUP_COMMANDS = ("clean", "purge")


def rule_command(identity: str) -> str:
    """Validate a full rule ID and return its owning command."""
    if not isinstance(identity, str) or not identity or _unsafe_text(identity):
        raise ValueError(f"invalid cleanup rule id: {identity!r}")
    prefix, separator, suffix = identity.partition(".")
    if prefix not in CLEANUP_COMMANDS or not separator or not suffix.strip():
        raise ValueError(
            f"rule id must start with clean. or purge. and have a nonempty suffix: {identity!r}"
        )
    if any(char.isspace() for char in identity) or any(
        not part for part in suffix.split(".")
    ):
        raise ValueError(
            f"rule id must not contain whitespace or empty dot-separated components: {identity!r}"
        )
    if suffix == "auto":
        raise ValueError(f"reserved cleanup rule id: {identity}")
    return prefix


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
    rules: tuple[str, ...] = ()
    scan_timeout: int = 0
    yes: bool = False


@dataclass(frozen=True)
class CleanRequest:
    agents: tuple[str, ...] = ()
    dry_run: bool = False
    config: Path | None = None
    rules: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()
    scan_timeout: int = 0
    yes: bool = False
    recursive: bool = False
    include_unverified: bool = False


@dataclass(frozen=True)
class ScanProgress:
    stage: str
    path: Path | None
    entries: int
    candidates: int
    elapsed_seconds: float
    git_queries: int = 0
    finished: bool = False
    stage_seconds: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class CleanupRule:
    id: str
    match: Condition
    exclude: tuple[str, ...] = ()
    enabled: bool = True
    auto_apply: bool = False
    recursive: bool = False
    include_unverified: bool = False
    min_age_days: int = 0
    preselect_after_days: int = 0
    keep_previous: int = 1
    origin: str = "builtin"

    def __post_init__(self) -> None:
        rule_command(self.id)
        if not isinstance(self.match, Condition):
            raise TypeError("match must be a parsed condition")

    @property
    def command(self) -> str:
        return rule_command(self.id)


@dataclass(frozen=True)
class ScanRule(CleanupRule):
    """One compiled rule routed to a discovery provider; never TOML input."""

    source: str = "paths"
    root: Path | None = None
    limits: tuple[Path, ...] = ()
    policies: tuple[CleanupRule, ...] = ()

    @property
    def is_project(self) -> bool:
        """Whether this provider requires project-context safety checks."""
        return self.source in {"project", "cachedir-tag"}

    @property
    def retained_versions(self) -> int:
        return max(
            (
                self.keep_previous,
                *(
                    policy.keep_previous
                    for policy in self.policies
                    if any(
                        entry.source == self.source for entry in entries(policy.match)
                    )
                ),
            )
        )

    @property
    def agent(self) -> str | None:
        return self.source.split(".")[0] if "." in self.source else None


@dataclass(frozen=True)
class RuleScope:
    rule: ScanRule
    root: Path


@dataclass(frozen=True)
class ConfigSignature:
    identity: tuple[int, ...]
    digest: str


@dataclass(frozen=True)
class RulesSnapshot:
    rules: tuple[CleanupRule, ...]
    files: tuple[tuple[Path, ConfigSignature | None], ...]
    expressions: Mapping[str, Condition]


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
    def purge_phase(self) -> bool:
        """Order by contributing rules, not the provider that discovered a file.

        A project provider can also discover an independent generic-file branch.
        Such a file still requires project checks but belongs to the later phase.
        """
        return any(identity.startswith("purge.") for identity in self.rule_ids)

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


@runtime_checkable
class CleanupPlanPresenter(Protocol):
    """Optional plan-presentation capability for cleanup UI adapters."""

    def present_plan(self, plan: CleanupPlan) -> None: ...


@runtime_checkable
class CleanupProgressObserver(Protocol):
    """Optional scan-progress capability for cleanup UI adapters."""

    def scan_progress(self, progress: ScanProgress) -> None: ...


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
    empty_directory_cascade: bool = False

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues) or any(
            result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
            for result in self.results
        )

    @property
    def exit_code(self) -> int:
        if any(issue.code == "scan_timeout" for issue in self.issues):
            return 1
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
    empty_directory_scopes: tuple[RuleScope, ...] = ()
    policy_scopes: tuple[RuleScope, ...] = ()


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
