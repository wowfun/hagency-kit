from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

from hagency_cli.files.cleanup import agents
from hagency_cli.files.cleanup.config import CleanupConfigError, load_rules
from hagency_cli.files.cleanup.inspection import (
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
from hagency_cli.files.cleanup.matching import exclusion_reason, relevant_scopes
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
    _BoundCandidate,
    _Identity,
    _TreeEntry,
    _unsafe_text,
)
from hagency_cli.files.cleanup.policy import age_policy, included, inspect_policy
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
    is_project = scopes[0].rule.command == "purge"
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
        git_contexts, git_error = _discover_git_contexts(path)
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
            tracked, git_error = _git_tracked_state(tracked_path, context.root)
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
        size, activity, error, hardlinks = _measure_candidate(
            path,
            now_ns,
            inspect_entry=inspect_policy(exclusions, agent=agent, project=is_project),
            tree=tree,
        )
        if error is None and tree:
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
            rule_ids=tuple(scope.rule.id for scope in scopes),
            rule_origins=tuple(dict.fromkeys(scope.rule.origin for scope in scopes)),
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
        return candidate, issues
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
) -> tuple[list[_BoundCandidate], list[CleanupIssue]]:
    candidates: list[_BoundCandidate] = []
    issues: list[CleanupIssue] = []
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
            for entry in children:
                checkpoint()
                path = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
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
                    if project is None and include_unverified:
                        # Without project evidence, patterns use this invocation's
                        # scan root. Keep that distinction in the frozen candidate.
                        project = root
                    all_scopes = (
                        tuple(RuleScope(rule, project or root) for rule in rules)
                        if path_scopes is None
                        else path_scopes
                    )
                    if project is not None and discovered_scopes is not None:
                        discovered_scopes.update(all_scopes)
                    scopes = tuple(
                        scope
                        for scope in all_scopes
                        if (path_scopes is None or scope.rule.source == "paths")
                        and included(scope, path, is_directory)
                    )
                    if scopes:
                        artifact_reason = None
                        protection_reason = None
                        if path_scopes is None:
                            artifact_reason = _artifact_verification_reason(path)
                            protection_reason = _context_protection_reason(
                                path, project or root
                            )
                            if protection_reason is None and not include_unverified:
                                reasons = []
                                if project is None:
                                    reasons.append("no qualifying project context")
                                if artifact_reason:
                                    reasons.append(artifact_reason)
                                protection_reason = "; ".join(reasons) or None
                        if protection_reason is None:
                            candidate, problems = _bind_candidate(
                                path,
                                root,
                                project,
                                scopes,
                                all_scopes,
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
                    if is_directory and (recursive or depth + 1 < MAX_SCAN_DEPTH):
                        stack.append((path, depth + 1))
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
    for candidate in ordered:
        identity: tuple[int, int] | str = (
            (candidate.target_identity.device, candidate.target_identity.inode)
            if candidate.target_identity.inode
            and stat.S_ISDIR(candidate.target_identity.mode)
            else _canonical_key(candidate.choice.exact_path)
        )
        if identity in identities:
            index = identities[identity]
            previous = kept[index]
            if previous.choice.exact_path == candidate.choice.exact_path:
                # Overlapping roots may find the same path with and without
                # enclosing evidence. Keep the verified ownership for revalidation.
                if (
                    candidate.choice.project_verified
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
        if any(
            _is_within(candidate.choice.exact_path, item.choice.exact_path)
            for item in kept
        ):
            continue
        identities[identity] = len(kept)
        kept.append(candidate)
    return kept


def _sort_candidates(
    candidates: list[_BoundCandidate],
) -> tuple[_BoundCandidate, ...]:
    project_totals: dict[Path, int] = {}
    seen_hardlinks: set[tuple[int, int]] = set()
    for candidate in sorted(candidates, key=lambda item: str(item.choice.exact_path)):
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
                -project_totals[item.choice.project_path],
                str(item.choice.project_path),
                -(item.choice.size_bytes if item.choice.size_bytes is not None else -1),
                str(item.choice.exact_path),
            ),
        )
    )


def _build_plan(request: PurgeRequest | CleanRequest) -> CleanupPlan:
    snapshot = load_rules(request.config)
    with mount_check_scope():
        return _plan_with_rules(request, snapshot)


def _plan_with_rules(
    request: PurgeRequest | CleanRequest, snapshot: RulesSnapshot
) -> CleanupPlan:
    now_ns = time.time_ns()
    command = "purge" if isinstance(request, PurgeRequest) else "clean"
    rules = tuple(
        rule for rule in snapshot.rules if rule.enabled and rule.command == command
    )
    if isinstance(request, PurgeRequest):
        roots, root_issues = _resolve_roots(request)
        candidates: list[_BoundCandidate] = []
        issues = list(root_issues)
        discovered_scopes: set[RuleScope] = set()
        for root in roots:
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
        # Different overlapping scan roots may discover different project owners.
        # Every discovered exclusion/retention constraint still applies globally.
        scopes = tuple(
            sorted(
                discovered_scopes, key=lambda scope: (str(scope.root), scope.rule.id)
            )
        )
        filtered = []
        for candidate in candidates:
            path = candidate.choice.exact_path
            applicable = relevant_scopes(scopes, path)
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
        candidates = filtered
    else:
        if not sys.platform.startswith("linux"):
            return CleanupPlan(
                (),
                (),
                (
                    CleanupIssue(
                        "unsupported_platform",
                        None,
                        "file clean currently supports Linux/WSL",
                    ),
                ),
                snapshot,
                command,
            )
        if any(agent not in agents.AGENTS for agent in request.agents):
            raise CleanupConfigError("unknown cleanup agent")
        selected_agents = request.agents or agents.AGENTS
        rules = tuple(
            rule
            for rule in rules
            if rule.agent is None or rule.agent in selected_agents
        )
        candidates, issues, roots = _agent_and_path_plan(rules, now_ns)
    return CleanupPlan(
        roots,
        _sort_candidates(_drop_duplicate_and_nested(candidates)),
        tuple(issues),
        snapshot,
        command,
    )


def _agent_and_path_plan(
    rules: tuple[CleanupRule, ...],
    now_ns: int,
) -> tuple[list[_BoundCandidate], list[CleanupIssue], tuple[Path, ...]]:
    candidates: list[_BoundCandidate] = []
    issues: list[CleanupIssue] = []
    all_scopes: list[RuleScope] = []
    roots: list[Path] = []
    for rule in rules:
        try:
            rule_roots = (
                (rule.root,)
                if rule.source == "paths"
                else agents.source_roots(rule.source)
            )
            all_scopes.extend(RuleScope(rule, root) for root in rule_roots)
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
            with scan_budget(agents.SOURCE_TIMEOUT):
                if source == "paths":
                    raw_roots = tuple(
                        dict.fromkeys(
                            scope.root
                            for scope in source_scopes
                            if scope.root.exists() or scope.root.is_symlink()
                        )
                    )
                    validated, root_issues = _validate_roots(raw_roots, Path.home())
                    issues.extend(root_issues)
                    for root in validated:
                        roots.append(root)
                        found, scan_issues = _scan_root(
                            root,
                            now_ns,
                            rules=source_rules,
                            recursive=True,
                            path_scopes=tuple(all_scopes),
                        )
                        candidates.extend(found)
                        issues.extend(scan_issues)
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
                    keep = max(rule.keep_previous for rule in source_rules)
                    inventory = agents.version_inventory(source, keep)
                else:
                    inventory = agents.staging_inventory()
                for entry in inventory:
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
                        if included(scope, entry.path, stat.S_ISDIR(info.st_mode))
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
