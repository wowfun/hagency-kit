from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from hagency_cli.files.cleanup import agents
from hagency_cli.files.cleanup.conditions import has_positive_empty_directory
from hagency_cli.files.cleanup.config import load_rules
from hagency_cli.files.cleanup.inspection import (
    ScanTimeout,
    _artifact_verification_reason,
    _canonical_key,
    _context_protection_reason,
    _discover_git_contexts,
    _find_project_root,
    _git_tracked_state,
    _identity,
    _is_mount,
    _is_reparse_stat,
    _is_within,
    _measure_candidate,
    _same_identity,
    checkpoint,
    mount_check_scope,
    scan_budget,
)
from hagency_cli.files.cleanup.matching import (
    ScopeIndex,
    exclusion_reason,
    relevant_scopes,
)
from hagency_cli.files.cleanup.models import (
    CACHEDIR_TAG_NAME,
    MAX_SCAN_DEPTH,
    SCAN_PRUNE_NAMES_CASEFOLD,
    Activity,
    CleanRequest,
    CleanupCandidate,
    CleanupIssue,
    CleanupPlan,
    CleanupRule,
    PurgeRequest,
    RuleScope,
    RulesSnapshot,
    ScanProgress,
    _BoundCandidate,
    _Identity,
    _TreeEntry,
    _unsafe_text,
)
from hagency_cli.files.cleanup.planning import current_session, planning_session
from hagency_cli.files.cleanup.policy import (
    age_policy,
    applicable_policies,
    included,
    inspect_policy,
)
from hagency_cli.files.cleanup.selection import select_rules
from hagency_cli.files.purge.roots import _resolve_roots, _validate_roots


def _candidate_id(path: Path, identity: _Identity) -> str:
    payload = f"{path}\0{identity.device}\0{identity.inode}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _bind_candidate(
    path: Path,
    root: Path,
    project: Path,
    scopes: tuple[RuleScope, ...],
    exclusions: tuple[RuleScope, ...],
    now_ns: int,
    *,
    project_verified: bool = True,
    artifact_unverified_reason: str | None = None,
) -> tuple[_BoundCandidate | None, list[CleanupIssue]]:
    issues: list[CleanupIssue] = []
    exclusions = relevant_scopes(exclusions, path)
    agent = scopes[0].rule.agent
    # Each discovery call binds homogeneous providers; deduplication may merge them later.
    is_project = scopes[0].rule.is_project
    try:
        checkpoint()
        if _unsafe_text(str(path)):
            return None, [
                CleanupIssue(
                    "protected",
                    path,
                    "control characters are not allowed in cleanup target paths",
                    False,
                )
            ]
        if agent:
            with agents.installation_lock(path, agent):
                pass
        root_identity = _identity(root)
        parent_identity = _identity(path.parent)
        target_identity = _identity(path)
        session = current_session()
        if session:
            session.update("git", path)
        git_contexts, git_error = (
            session.git_contexts(path) if session else _discover_git_contexts(path)
        )
        if git_error is not None:
            return None, [
                CleanupIssue(
                    "git_check_failed",
                    path,
                    f"could not prove candidate is untracked: {git_error}",
                )
            ]
        for context in git_contexts:
            tracked_path = (
                path
                if _is_within(path, context.root, allow_equal=True)
                else context.root
            )
            tracked, git_error = (
                session.git_tracked(tracked_path, context.root)
                if session
                else _git_tracked_state(tracked_path, context.root)
            )
            if tracked is None:
                return None, [
                    CleanupIssue(
                        "git_check_failed",
                        path,
                        f"could not prove candidate is untracked: {git_error}",
                    )
                ]
            if tracked:
                return None, [
                    CleanupIssue(
                        "protected", path, "Git-tracked content is protected", False
                    )
                ]
        tree: list[_TreeEntry] = []
        if session:
            session.update("measuring", path)
        size, activity, error, hardlinks = _measure_candidate(
            path,
            now_ns,
            inspect_entry=inspect_policy(exclusions, agent=agent, project=is_project),
            tree=tree,
        )
        if error is None and tree:
            if not all(
                included(scope, path, tree[0].identity.mode, tree[0].size)
                for scope in scopes
            ):
                return None, [
                    CleanupIssue(
                        "candidate_changed_during_scan",
                        path,
                        "candidate no longer matches its cleanup rules after inspection",
                    )
                ]
            activity, error = age_policy(path, tree, scopes, exclusions, now_ns)
        if error:
            protected = error.startswith("protected:")
            issues.append(
                CleanupIssue(
                    "protected" if protected else "candidate_measure_failed",
                    path,
                    error,
                    not protected,
                )
            )
            if protected:
                return None, issues
        if (
            size == 0
            and stat.S_ISDIR(target_identity.mode)
            and all(
                scope.rule.id in {"purge.artifacts", "purge.cachedirs"}
                for scope in scopes
            )
        ):
            return None, issues
        tagged = all(scope.rule.source == "cachedir-tag" for scope in scopes)
        contributors = tuple(
            dict.fromkeys(
                policy
                for scope in scopes
                for policy in applicable_policies(
                    scope, path, target_identity.mode, tree[0].size if tree else 0
                )
            )
        )
        choice = CleanupCandidate(
            id=_candidate_id(path, target_identity),
            exact_path=path,
            project_path=project,
            artifact_kind=CACHEDIR_TAG_NAME if tagged else path.name,
            size_bytes=size,
            activity=activity,
            preselected=project_verified
            and not _is_within(Path.home(), root)
            and artifact_unverified_reason is None
            and activity is Activity.OLD
            and size is not None,
            rule_ids=tuple(dict.fromkeys(rule.id for rule in contributors)),
            rule_origins=tuple(dict.fromkeys(rule.origin for rule in contributors)),
            entry_kind="directory" if stat.S_ISDIR(target_identity.mode) else "file",
            group=agent or (str(project) if is_project else str(root)),
            project_verified=project_verified,
            artifact_unverified_reason=artifact_unverified_reason,
        )
        candidate = _BoundCandidate(
            choice,
            root,
            root_identity,
            parent_identity,
            target_identity,
            git_contexts,
            hardlinks,
            scopes,
            exclusions,
            agent,
            tuple(tree),
        )
        if (
            not _same_identity(root, root_identity)
            or not _same_identity(path.parent, parent_identity)
            or not _same_identity(path, target_identity)
        ):
            return None, [
                *issues,
                CleanupIssue(
                    "candidate_changed_during_scan",
                    path,
                    "candidate or parent changed before scan results were published",
                ),
            ]
        checkpoint()
        if session:
            session.candidates += 1
        return candidate, issues
    except ScanTimeout:
        raise
    except OSError as exc:
        return None, [*issues, CleanupIssue("candidate_stat_failed", path, str(exc))]


def _scan_root(
    root: Path,
    now_ns: int,
    *,
    rules: tuple[CleanupRule, ...],
    recursive: bool = False,
    include_unverified: bool = False,
    path_scopes: tuple[RuleScope, ...] | None = None,
    discovered_scopes: set[RuleScope] | None = None,
    covered_directories: frozenset[Path] = frozenset(),
    scanned_directories: set[Path] | None = None,
) -> tuple[list[_BoundCandidate], list[CleanupIssue]]:
    candidates: list[_BoundCandidate] = []
    issues: list[CleanupIssue] = []
    root_identity = None
    # These are completed, protected phase-one candidates, never deletion authority.
    if root in covered_directories or any(
        parent in covered_directories for parent in root.parents
    ):
        return candidates, issues
    if scanned_directories is not None and root in scanned_directories:
        return candidates, issues
    recursive_rules = tuple(rule for rule in rules if rule.recursive)
    unverified_any = include_unverified or any(
        rule.include_unverified for rule in rules
    )
    fixed_scopes = all(rule.root is not None for rule in rules)
    is_windows = sys.platform == "win32"
    scopes_by_root: dict[Path, tuple[RuleScope, ...]] = {}
    registered_roots: set[Path] = set()
    path_index = ScopeIndex(path_scopes) if path_scopes is not None else None
    matching_path_scopes = tuple(
        scope for scope in path_scopes or () if scope.rule.source == "paths"
    )
    if _is_within(Path.home(), root):
        issues.append(
            CleanupIssue(
                "broad_root",
                root,
                "scan root contains the home directory; all candidates require manual selection",
                False,
            )
        )
    try:
        root_identity = _identity(root)
        visited = {(root_identity.device, root_identity.inode)}
        stack = [(root, 0)]
        while stack:
            checkpoint()
            directory, depth = stack.pop()
            if scanned_directories is not None and directory in scanned_directories:
                continue
            try:
                with os.scandir(directory) as entries:
                    children = list(entries)
            except OSError as exc:
                issues.append(
                    CleanupIssue(
                        "scan_failed", directory, f"could not scan directory: {exc}"
                    )
                )
                continue
            if scanned_directories is not None:
                scanned_directories.add(directory)
            for entry in children:
                path = Path(entry.path)
                session = current_session()
                if session:
                    session.entries += 1
                    session.update("scanning", path)
                checkpoint()
                try:
                    if path in covered_directories:
                        continue
                    # Windows DirEntry.stat() omits device and inode identity.
                    info = (
                        path.stat(follow_symlinks=False)
                        if is_windows
                        else entry.stat(follow_symlinks=False)
                    )
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or _is_reparse_stat(info)
                        or not (is_directory or stat.S_ISREG(info.st_mode))
                    ):
                        continue
                    if (
                        entry.name.casefold() in SCAN_PRUNE_NAMES_CASEFOLD
                        or entry.name.startswith(".hagency-cleanup-")
                    ):
                        continue
                    if is_directory:
                        if not info.st_ino:
                            raise OSError(
                                f"stable filesystem identity is unavailable for {path}"
                            )
                        if info.st_dev != root_identity.device or _is_mount(path):
                            issues.append(
                                CleanupIssue(
                                    "protected",
                                    path,
                                    "child mount point is protected",
                                    False,
                                )
                            )
                            continue
                        identity = (info.st_dev, info.st_ino)
                        if identity in visited:
                            continue
                        visited.add(identity)
                    project = (
                        _find_project_root(path, root) if path_scopes is None else root
                    )
                    project_verified = project is not None
                    if project is None and unverified_any:
                        # Without project evidence, patterns use this invocation's
                        # scan root. Keep that distinction in the frozen candidate.
                        project = root
                    if path_scopes is None:
                        scope_root = root if fixed_scopes else project or root
                        if scope_root not in scopes_by_root:
                            scopes_by_root[scope_root] = tuple(
                                RuleScope(rule, rule.root or project or root)
                                for rule in rules
                            )
                        all_scopes = scopes_by_root[scope_root]
                    else:
                        all_scopes = path_scopes
                    if (
                        path_scopes is None
                        and project is not None
                        and discovered_scopes is not None
                        and scope_root not in registered_roots
                    ):
                        discovered_scopes.update(all_scopes)
                        registered_roots.add(scope_root)
                    scopes = tuple(
                        scope
                        for scope in (
                            all_scopes if path_scopes is None else matching_path_scopes
                        )
                        if (
                            path_scopes is not None
                            or recursive
                            or scope.rule.recursive
                            or depth < MAX_SCAN_DEPTH
                        )
                        and included(scope, path, info.st_mode, info.st_size)
                    )
                    if scopes:
                        artifact_reason = None
                        protection_reason = None
                        if path_scopes is None:
                            artifact_reason = _artifact_verification_reason(path)
                            protection_reason = _context_protection_reason(
                                path, project or root
                            )
                            if protection_reason is None:
                                reasons = []
                                if not project_verified:
                                    reasons.append("no qualifying project context")
                                if artifact_reason:
                                    reasons.append(artifact_reason)
                                if reasons and not include_unverified:
                                    permitted = tuple(
                                        scope
                                        for scope in scopes
                                        if scope.rule.include_unverified
                                    )
                                    if permitted:
                                        scopes = permitted
                                    else:
                                        protection_reason = "; ".join(reasons)
                        if protection_reason is None:
                            candidate, problems = _bind_candidate(
                                path,
                                root,
                                project,
                                scopes,
                                path_index.for_path(path)
                                if path_index is not None
                                else all_scopes,
                                now_ns,
                                project_verified=project_verified,
                                artifact_unverified_reason=artifact_reason,
                            )
                            issues.extend(problems)
                            if candidate is not None:
                                candidates.append(candidate)
                        else:
                            issues.append(
                                CleanupIssue(
                                    "protected",
                                    path,
                                    protection_reason,
                                    False,
                                )
                            )
                        # Whole artifact directories are never traversed, including protected ones.
                        continue
                    if is_directory and (
                        recursive
                        or depth + 1 < MAX_SCAN_DEPTH
                        or any(
                            (
                                rule.root is None
                                or path.is_relative_to(rule.root)
                                or rule.root.is_relative_to(path)
                            )
                            and (
                                not rule.limits
                                or any(
                                    path.is_relative_to(limit)
                                    or limit.is_relative_to(path)
                                    for limit in rule.limits
                                )
                            )
                            for rule in recursive_rules
                        )
                    ):
                        stack.append((path, depth + 1))
                except ScanTimeout:
                    raise
                except OSError as exc:
                    issues.append(CleanupIssue("scan_stat_failed", path, str(exc)))
        if not _same_identity(root, root_identity):
            return [], [
                *issues,
                CleanupIssue(
                    "root_changed_during_scan",
                    root,
                    "scan root changed before its results could be published",
                ),
            ]
        return candidates, issues
    except ScanTimeout as exc:
        session = current_session()
        if session is None:
            raise
        session.timed_out(exc, root)
        if root_identity is None:
            return [], issues
        if not _same_identity(root, root_identity):
            return [], [
                *issues,
                CleanupIssue("root_changed_during_scan", root, "scan root changed"),
            ]
        return candidates, issues
    except OSError as exc:
        return [], [*issues, CleanupIssue("scan_failed", root, str(exc))]


def _drop_duplicate_and_nested(
    candidates: list[_BoundCandidate],
) -> list[_BoundCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            len(item.choice.exact_path.parts),
            str(item.choice.exact_path),
        ),
    )
    kept: list[_BoundCandidate] = []
    identities: dict[tuple[int, int] | str, int] = {}
    directories: set[str] = set()
    for candidate in ordered:
        checkpoint()
        # normcase folds on Windows; POSIX (including macOS) preserves case.
        # Use the same canonical spelling for candidates and ancestor keys.
        canonical = _canonical_key(candidate.choice.exact_path)
        identity: tuple[int, int] | str = (
            (candidate.target_identity.device, candidate.target_identity.inode)
            if candidate.target_identity.inode
            and stat.S_ISDIR(candidate.target_identity.mode)
            else canonical
        )
        if identity in identities:
            index = identities[identity]
            previous = kept[index]
            if previous.choice.exact_path == candidate.choice.exact_path:
                # A generic path's synthetic owner is not project evidence.
                # Preserve the project scan's real context when combining sources;
                # between equivalent providers, prefer verified ownership.
                previous_project = any(
                    scope.rule.is_project for scope in previous.scopes
                )
                candidate_project = any(
                    scope.rule.is_project for scope in candidate.scopes
                )
                if (candidate_project and not previous_project) or (
                    candidate_project == previous_project
                    and candidate.choice.project_verified
                    and not previous.choice.project_verified
                ):
                    previous, candidate = candidate, previous
                choice = replace(
                    previous.choice,
                    rule_ids=tuple(
                        dict.fromkeys(
                            (*previous.choice.rule_ids, *candidate.choice.rule_ids)
                        )
                    ),
                    rule_origins=tuple(
                        dict.fromkeys(
                            (
                                *previous.choice.rule_origins,
                                *candidate.choice.rule_origins,
                            )
                        )
                    ),
                    preselected=previous.choice.preselected
                    and candidate.choice.preselected,
                )
                kept[index] = replace(
                    previous,
                    choice=choice,
                    scopes=tuple(dict.fromkeys((*previous.scopes, *candidate.scopes))),
                )
            continue
        if any(str(parent) in directories for parent in Path(canonical).parents):
            continue
        identities[identity] = len(kept)
        kept.append(candidate)
        if stat.S_ISDIR(candidate.target_identity.mode):
            directories.add(canonical)
    return kept


def _sort_candidates(
    candidates: list[_BoundCandidate],
) -> tuple[_BoundCandidate, ...]:
    project_totals: dict[Path, int] = {}
    seen_hardlinks: set[tuple[int, int]] = set()
    for candidate in sorted(candidates, key=lambda item: str(item.choice.exact_path)):
        checkpoint()
        project = candidate.choice.project_path
        accounted_size = candidate.choice.size_bytes or 0
        for entry in candidate.hardlink_entries:
            if entry.identity is None:
                continue
            if entry.identity in seen_hardlinks:
                accounted_size -= entry.size_bytes
            else:
                seen_hardlinks.add(entry.identity)
        project_totals[project] = project_totals.get(project, 0) + max(
            accounted_size, 0
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                not item.choice.purge_phase,
                -project_totals[item.choice.project_path],
                str(item.choice.project_path),
                -(item.choice.size_bytes if item.choice.size_bytes is not None else -1),
                str(item.choice.exact_path),
            ),
        )
    )


def _build_plan(
    request: PurgeRequest | CleanRequest,
    progress: Callable[[ScanProgress], None] | None = None,
) -> CleanupPlan:
    snapshot = load_rules(request.config)
    rules = select_rules(request, snapshot)
    with planning_session(request.scan_timeout, progress), mount_check_scope():
        return _plan_with_rules(request, snapshot, rules)


def _project_plan(
    request: PurgeRequest | CleanRequest,
    rules: tuple[CleanupRule, ...],
    now_ns: int,
    extra_scopes: tuple[RuleScope, ...] = (),
) -> tuple[
    list[_BoundCandidate], list[CleanupIssue], tuple[Path, ...], tuple[RuleScope, ...]
]:
    session = current_session()
    assert session is not None
    if any(rule.root is None for rule in rules):
        # Clean rootless entries use explicit PATH only; purge retains saved/home discovery.
        if isinstance(request, CleanRequest):
            fallback, root_issues = request.paths, ()
        else:
            fallback, root_issues = _resolve_roots(request)
    else:
        fallback, root_issues = (), ()
    raw_roots = tuple(
        dict.fromkeys(
            root
            for rule in rules
            for base in ((rule.root,) if rule.root is not None else fallback)
            for root in _intersect_roots(base, rule.limits)
        )
    )
    roots, validation_issues = _validate_roots(raw_roots, Path.home())
    root_issues = (*root_issues, *validation_issues)
    candidates: list[_BoundCandidate] = []
    issues = list(root_issues)
    discovered_scopes: set[RuleScope] = set()
    for index, root in enumerate(roots):
        session.remaining_roots = roots[index:]
        with session.budget():
            found, scan_issues = _scan_root(
                root,
                now_ns,
                rules=rules,
                recursive=request.recursive,
                include_unverified=request.include_unverified,
                discovered_scopes=discovered_scopes,
            )
        candidates.extend(found)
        issues.extend(scan_issues)
        if session.timeout_issues:
            break
    if not session.timeout_issues:
        session.remaining_roots = ()
    # Different overlapping scan roots may discover different project owners.
    # Every discovered exclusion/retention constraint still applies globally.
    scopes = (
        *extra_scopes,
        *tuple(
            sorted(
                discovered_scopes, key=lambda scope: (str(scope.root), scope.rule.id)
            )
        ),
    )
    scope_index = ScopeIndex(scopes)
    filtered = []
    try:
        with session.budget() if not session.timeout_issues else scan_budget(None):
            for candidate in candidates:
                path = candidate.choice.exact_path
                session.update("protecting", path)
                checkpoint()
                try:
                    applicable = scope_index.for_path(path)
                    reason = next(
                        (
                            reason
                            for entry in candidate.tree
                            if (
                                reason := exclusion_reason(
                                    applicable,
                                    path / entry.relative_path,
                                    stat.S_ISDIR(entry.identity.mode),
                                )
                            )
                        ),
                        None,
                    )
                    activity = candidate.choice.activity
                    if reason is None and candidate.tree:
                        activity, reason = age_policy(
                            path,
                            list(candidate.tree),
                            candidate.scopes,
                            applicable,
                            now_ns,
                        )
                except ScanTimeout:
                    raise
                except OSError as exc:
                    # A failed predicate cannot prove that retention is satisfied.
                    # Reject this candidate without discarding other checked items.
                    issues.append(
                        CleanupIssue(
                            "candidate_policy_failed",
                            path,
                            f"could not verify candidate protection policies: {exc}",
                        )
                    )
                    continue
                if reason:
                    issues.append(CleanupIssue("protected", path, reason, False))
                else:
                    choice = replace(
                        candidate.choice,
                        activity=activity,
                        preselected=activity is Activity.OLD
                        and not _is_within(Path.home(), candidate.root)
                        and candidate.choice.size_bytes is not None
                        and candidate.choice.project_verified
                        and candidate.choice.artifact_unverified_reason is None,
                    )
                    filtered.append(
                        replace(candidate, choice=choice, exclusions=applicable)
                    )
    except ScanTimeout as exc:
        session.timed_out(exc, session.path)
    candidates = filtered
    return candidates, issues, roots, scopes


def _plan_with_rules(
    request: PurgeRequest | CleanRequest,
    snapshot: RulesSnapshot,
    rules: tuple[CleanupRule, ...],
) -> CleanupPlan:
    now_ns = time.time_ns()
    session = current_session()
    assert session is not None
    command = "purge" if isinstance(request, PurgeRequest) else "clean"
    if not rules:
        return CleanupPlan(
            (),
            (),
            (
                CleanupIssue(
                    "no_rules_selected",
                    None,
                    f"No {command} rules selected; inspect hgc file rule --command {command} "
                    "or select a rule with --rule ID",
                    False,
                ),
            ),
            snapshot,
            command,
        )
    empty_directory_scopes: tuple[RuleScope, ...] = ()
    policy_scopes: tuple[RuleScope, ...] = ()
    if isinstance(request, PurgeRequest):
        candidates, issues, roots, _ = _project_plan(request, rules, now_ns)
    else:
        if sys.platform == "win32":
            unsupported = tuple(rule for rule in rules if rule.agent is not None)
        elif sys.platform.startswith("linux"):
            unsupported = ()
        else:
            unsupported = rules
        if unsupported:
            labels = tuple(
                dict.fromkeys(f"{rule.id} [{rule.source}]" for rule in unsupported)
            )
            names = ", ".join(
                label if len(label) <= 160 else label[:157] + "..."
                for label in labels[:5]
            )
            if len(labels) > 5:
                names += f", ... ({len(labels) - 5} more)"
            reason = (
                "Selected Agent cleanup rules require Linux/WSL"
                if sys.platform == "win32"
                else "file clean supports Linux/WSL and Windows project/path rules"
            )
            return CleanupPlan(
                (),
                (),
                (
                    CleanupIssue("unsupported_platform", None, f"{reason}: {names}"),
                ),
                snapshot,
                command,
            )
        project_rules = tuple(
            rule for rule in rules if rule.source in {"project", "cachedir-tag"}
        )
        other_rules = tuple(
            rule for rule in rules if rule.source not in {"project", "cachedir-tag"}
        )
        path_scopes = tuple(
            RuleScope(rule, root)
            for rule in other_rules
            if rule.source == "paths"
            for root in ((rule.root,) if rule.root is not None else rule.limits)
        )
        candidates, issues, roots, project_scopes = [], [], (), ()
        if project_rules:
            candidates, issues, roots, project_scopes = _project_plan(
                request,
                project_rules,
                now_ns,
                path_scopes,
            )
        found, problems, other_roots = _agent_and_path_plan(
            other_rules,
            now_ns,
            request.paths,
            extra_scopes=project_scopes,
            covered_directories=frozenset(
                item.choice.exact_path
                for item in candidates
                if item.choice.entry_kind == "directory"
                and item.choice.size_bytes is not None
                and item.tree
            ),
        )
        candidates.extend(found)
        issues.extend(problems)
        roots = tuple(dict.fromkeys((*roots, *other_roots)))
        policy_scopes = tuple(dict.fromkeys((*project_scopes, *path_scopes)))
        empty_directory_scopes = tuple(
            scope
            for scope in path_scopes
            if has_positive_empty_directory(scope.rule.match)
        )
    session.update("finalizing")
    try:
        with (
            session.budget()
            if not session.timeout_issues and any(rule.agent is None for rule in rules)
            else scan_budget(None)
        ):
            checkpoint()
            finalized = _sort_candidates(_drop_duplicate_and_nested(candidates))
            checkpoint()
    except ScanTimeout as exc:
        session.timed_out(exc, None)
        # Finish only the already inspected candidates; this cannot discover new targets.
        with scan_budget(None):
            finalized = _sort_candidates(_drop_duplicate_and_nested(candidates))
    session.candidates = len(finalized)
    return CleanupPlan(
        roots,
        finalized,
        (
            *issues,
            *(
                replace(
                    issue,
                    message=f"{issue.message} {session.entries} entries inspected, {len(finalized)} complete candidates, {time.monotonic() - session.started:.1f}s elapsed.",
                )
                for issue in session.timeout_issues
            ),
        ),
        snapshot,
        command,
        empty_directory_scopes,
        policy_scopes,
    )


def _intersect_roots(root: Path, limits: tuple[Path, ...]) -> tuple[Path, ...]:
    if not limits:
        return (root,)
    return tuple(
        dict.fromkeys(
            limit if limit.is_relative_to(root) else root
            for limit in limits
            if limit.is_relative_to(root) or root.is_relative_to(limit)
        )
    )


def _agent_and_path_plan(
    rules: tuple[CleanupRule, ...],
    now_ns: int,
    paths: tuple[Path, ...] = (),
    *,
    extra_scopes: tuple[RuleScope, ...] = (),
    covered_directories: frozenset[Path] = frozenset(),
) -> tuple[list[_BoundCandidate], list[CleanupIssue], tuple[Path, ...]]:
    candidates: list[_BoundCandidate] = []
    issues: list[CleanupIssue] = []
    all_scopes: list[RuleScope] = list(extra_scopes)
    roots: list[Path] = []
    session = current_session()
    assert session is not None
    source_roots: dict[str, tuple[Path, ...]] = {}
    for rule in rules:
        try:
            if rule.agent and rule.source not in source_roots:
                source_roots[rule.source] = agents.source_roots(rule.source)
            rule_roots = (
                ((rule.root,) if rule.root is not None else rule.limits)
                if rule.source == "paths"
                else source_roots[rule.source]
            )
            all_scopes.extend(
                RuleScope(rule, root)
                for root in rule_roots
                if not rule.agent
                or (
                    _intersect_roots(root, rule.limits)
                    and (rule.root is None or _intersect_roots(root, (rule.root,)))
                )
            )
        except OSError as exc:
            issues.append(CleanupIssue("source_failed", None, f"{rule.id}: {exc}"))
    for source in dict.fromkeys(rule.source for rule in rules):
        source_scopes = tuple(
            scope for scope in all_scopes if scope.rule.source == source
        )
        if not source_scopes:
            continue
        source_rules = tuple(dict.fromkeys(scope.rule for scope in source_scopes))
        try:
            with (
                session.budget()
                if source == "paths"
                else scan_budget(agents.SOURCE_TIMEOUT)
            ):
                if source == "paths":
                    raw_roots = tuple(
                        dict.fromkeys(
                            root
                            for scope in source_scopes
                            for root in _intersect_roots(scope.root, scope.rule.limits)
                            if root.exists() or root.is_symlink()
                        )
                    )
                    validated, root_issues = _validate_roots(raw_roots, Path.home())
                    issues.extend(root_issues)
                    # Share actual visits, not just containing roots: an outer
                    # scan may stop at a protected directory that is itself an
                    # explicitly supplied inner root and still needs inspection.
                    scanned_directories: set[Path] = set()
                    for index, root in enumerate(validated):
                        session.remaining_roots = validated[index:]
                        roots.append(root)
                        found, scan_issues = _scan_root(
                            root,
                            now_ns,
                            rules=source_rules,
                            recursive=True,
                            path_scopes=tuple(all_scopes),
                            covered_directories=covered_directories,
                            scanned_directories=scanned_directories,
                        )
                        candidates.extend(found)
                        issues.extend(scan_issues)
                        if session.timeout_issues:
                            break
                    if not session.timeout_issues:
                        session.remaining_roots = ()
                    continue
                agent = source_rules[0].agent
                if not any(
                    scope.root.exists() or scope.root.is_symlink()
                    for scope in source_scopes
                ):
                    continue
                reason = agents.process_block_reason(agent)
                if reason:
                    issues.append(
                        CleanupIssue(
                            "agent_busy",
                            source_scopes[0].root,
                            reason,
                            "unknown" in reason,
                        )
                    )
                    continue
                if source.endswith(".versions"):
                    keep = max(rule.retained_versions for rule in source_rules)
                    inventory = agents.version_inventory(source, keep)
                else:
                    inventory = agents.staging_inventory()
                for entry in inventory:
                    session.entries += 1
                    session.update("scanning", entry.path)
                    checkpoint()
                    if entry.root not in roots:
                        roots.append(entry.root)
                    if entry.reason:
                        issues.append(
                            CleanupIssue("protected", entry.path, entry.reason, False)
                        )
                        continue
                    try:
                        info = entry.path.lstat()
                    except FileNotFoundError:
                        # Retention has already been computed from a complete
                        # inventory. Losing an eligible target cannot relax it;
                        # deletion still revalidates the remaining inventory.
                        issues.append(
                            CleanupIssue(
                                "candidate_vanished",
                                entry.path,
                                "eligible target disappeared during scanning",
                                False,
                            )
                        )
                        continue
                    scopes = tuple(
                        scope
                        for scope in source_scopes
                        if included(scope, entry.path, info.st_mode, info.st_size)
                    )
                    if not scopes:
                        continue
                    reason = agents.occupancy_block_reason(entry.path)
                    if reason:
                        issues.append(
                            CleanupIssue(
                                "agent_busy", entry.path, reason, "unknown" in reason
                            )
                        )
                        continue
                    if _is_mount(entry.path):
                        issues.append(
                            CleanupIssue(
                                "protected",
                                entry.path,
                                "mount point is protected",
                                False,
                            )
                        )
                        continue
                    candidate, problems = _bind_candidate(
                        entry.path,
                        entry.root,
                        entry.root,
                        scopes,
                        tuple(all_scopes),
                        now_ns,
                    )
                    issues.extend(problems)
                    if candidate is not None:
                        candidates.append(candidate)
        except ScanTimeout as exc:
            if source == "paths":
                session.timed_out(exc, source_scopes[0].root)
            else:
                candidates = [
                    candidate
                    for candidate in candidates
                    if all(scope.rule.source != source for scope in candidate.scopes)
                ]
                issues.append(
                    CleanupIssue(
                        "source_failed", source_scopes[0].root, f"{source}: {exc}"
                    )
                )
        except OSError as exc:
            # A source whose state cannot be fully inspected contributes no targets.
            candidates = [
                candidate
                for candidate in candidates
                if all(scope.rule.source != source for scope in candidate.scopes)
            ]
            issues.append(
                CleanupIssue("source_failed", source_scopes[0].root, f"{source}: {exc}")
            )
    return candidates, issues, tuple(dict.fromkeys(roots))
