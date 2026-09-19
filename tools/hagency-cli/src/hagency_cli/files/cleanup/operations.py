from __future__ import annotations

import heapq
import os
import stat
import time
from dataclasses import replace
from pathlib import Path

from hagency_cli.files.cleanup.agents import installation_lock
from hagency_cli.files.cleanup.config import CleanupConfigError, configuration_unchanged
from hagency_cli.files.cleanup.inspection import scan_budget
from hagency_cli.files.cleanup.matching import ScopeIndex
from hagency_cli.files.cleanup.models import (
    CleanRequest,
    CleanupDisposition,
    CleanupIssue,
    CleanupItemResult,
    CleanupPlan,
    CleanupPlanPresenter,
    CleanupProgressObserver,
    CleanupReport,
    CleanupUI,
    ItemDisposition,
    PurgeRequest,
    _BoundCandidate,
)
from hagency_cli.files.cleanup.policy import included
from hagency_cli.files.cleanup.removal import _permanently_remove, _revalidate
from hagency_cli.files.cleanup.scan import _bind_candidate, _build_plan


def _partial_or(
    disposition: CleanupDisposition,
    issues: tuple[CleanupIssue, ...],
    results: tuple[CleanupItemResult, ...] = (),
) -> CleanupDisposition:
    if disposition in {CleanupDisposition.PREVIEW, CleanupDisposition.CANCELLED}:
        return disposition
    if any(issue.is_failure for issue in issues) or any(
        result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
        for result in results
    ):
        return CleanupDisposition.PARTIAL
    return disposition


def _selection_known_bytes(selected: tuple[_BoundCandidate, ...]) -> int:
    seen: set[tuple[int, int]] = set()
    total = sum(
        candidate.choice.size_bytes or 0
        for candidate in selected
        if candidate.choice.size_bytes is not None
    )
    for candidate in selected:
        if candidate.choice.size_bytes is None:
            continue
        for entry in candidate.hardlink_entries:
            if entry.identity in seen:
                total -= entry.size_bytes
            else:
                seen.add(entry.identity)
    return total


def _make_report(
    plan: CleanupPlan,
    disposition: CleanupDisposition,
    *,
    selected: tuple[_BoundCandidate, ...] = (),
    results: tuple[CleanupItemResult, ...] = (),
    extra_issues: tuple[CleanupIssue, ...] = (),
) -> CleanupReport:
    issues = (*plan.issues, *extra_issues)
    return CleanupReport(
        disposition=_partial_or(disposition, issues, results),
        roots=plan.roots,
        choices=tuple(item.choice for item in plan.candidates),
        selected_paths=tuple(item.choice.exact_path for item in selected),
        results=results,
        issues=issues,
        known_bytes=_selection_known_bytes(selected),
        command=plan.command,
        config_files=tuple(path for path, _signature in plan.snapshot.files)
        if plan.snapshot
        else (),
        empty_directory_cascade=bool(plan.empty_directory_scopes),
    )


def _bind_selection(
    ids: tuple[str, ...], plan: CleanupPlan
) -> tuple[tuple[_BoundCandidate, ...], tuple[CleanupIssue, ...]]:
    by_id = {candidate.choice.id: candidate for candidate in plan.candidates}
    if len(ids) != len(set(ids)):
        return (), (
            CleanupIssue("invalid_selection", None, "selection contains duplicate IDs"),
        )
    unknown = [value for value in ids if value not in by_id]
    if unknown:
        return (), (
            CleanupIssue(
                "invalid_selection",
                None,
                f"selection contains unknown IDs: {', '.join(unknown)}",
            ),
        )
    return tuple(by_id[value] for value in ids), ()


def _cascade_empty_directories(
    plan: CleanupPlan,
    removed_paths: tuple[Path, ...],
    removed_links: set[tuple[int, int]],
) -> tuple[
    tuple[_BoundCandidate, ...], tuple[CleanupItemResult, ...], tuple[CleanupIssue, ...]
]:
    """Remove newly empty ancestors without rediscovering unrelated directories."""
    if not plan.empty_directory_scopes:
        return (), (), ()

    selected: list[_BoundCandidate] = []
    results: list[CleanupItemResult] = []
    issues: list[CleanupIssue] = []
    scope_index = ScopeIndex(tuple(dict.fromkeys(plan.empty_directory_scopes)))
    policy_index = ScopeIndex(plan.policy_scopes)
    removed_identities = frozenset(removed_links)
    pending: list[tuple[int, int, Path]] = []
    scheduled: set[Path] = set()

    def schedule(path: Path, order: int) -> None:
        if path not in scheduled:
            scheduled.add(path)
            heapq.heappush(pending, (-len(path.parts), order, path))

    for order, removed in enumerate(removed_paths):
        schedule(removed.parent, order)
    # Always finish deeper siblings before their parent. A parent is checked once,
    # after every selected branch that could make it empty has completed.
    while pending:
        _depth, order, path = heapq.heappop(pending)
        possible_scopes = tuple(
            scope
            for scope in scope_index.for_path(path)
            if path != scope.root and path.is_relative_to(scope.root)
        )
        if not possible_scopes:
            continue
        candidate = None
        try:
            # Binding and deletion share this ancestor's bounded, fresh mount view;
            # no filesystem or Git authorization survives a preceding removal.
            with scan_budget():
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                with os.scandir(path) as entries:
                    if next(entries, None) is not None:
                        continue
                scopes = tuple(
                    scope
                    for scope in possible_scopes
                    if included(scope, path, info.st_mode, info.st_size)
                )
                if not scopes:
                    continue
                # The containing root bounds identity/containment checks only.
                # Each RuleScope retains its own glob base in included()/policy.
                root = min(
                    (scope.root for scope in scopes), key=lambda value: len(value.parts)
                )
                candidate, problems = _bind_candidate(
                    path,
                    root,
                    root,
                    scopes,
                    policy_index.for_path(path),
                    time.time_ns(),
                )
                issues.extend(problems)
                if candidate is None:
                    continue
                # Other OR branches may match nonempty directories. A cascade
                # may authorize only the empty directory itself, even after a race.
                if len(candidate.tree) != 1 or candidate.tree[0].relative_path != ".":
                    results.append(
                        CleanupItemResult(
                            path,
                            ItemDisposition.SKIPPED,
                            candidate.choice.size_bytes,
                            "directory is no longer verifiably empty; preview again",
                        )
                    )
                    continue
                candidate = replace(candidate, removed_links=removed_identities)
                if plan.snapshot is not None and not configuration_unchanged(
                    plan.snapshot
                ):
                    issues.append(
                        CleanupIssue(
                            "config_changed",
                            None,
                            "cleanup configuration changed before empty-directory cascade; preview again",
                        )
                    )
                    return tuple(selected), tuple(results), tuple(issues)
                with installation_lock(candidate.choice.exact_path, candidate.agent):
                    reason = _revalidate(candidate)
                    if reason is not None:
                        results.append(
                            CleanupItemResult(
                                path,
                                ItemDisposition.SKIPPED,
                                candidate.choice.size_bytes,
                                reason,
                            )
                        )
                        continue
                    if plan.snapshot is not None and not configuration_unchanged(
                        plan.snapshot
                    ):
                        issues.append(
                            CleanupIssue(
                                "config_changed",
                                None,
                                "cleanup configuration changed during empty-directory cascade; preview again",
                            )
                        )
                        return tuple(selected), tuple(results), tuple(issues)
                    _permanently_remove(candidate)
        except OSError as exc:
            if candidate is None:
                issues.append(
                    CleanupIssue(
                        "cascade_check_failed",
                        path,
                        f"could not verify empty directory: {exc}",
                    )
                )
            else:
                results.append(
                    CleanupItemResult(
                        path,
                        ItemDisposition.FAILED,
                        candidate.choice.size_bytes,
                        str(exc),
                    )
                )
            continue
        selected.append(candidate)
        results.append(
            CleanupItemResult(
                path,
                ItemDisposition.REMOVED,
                candidate.choice.size_bytes,
                "removed after selected descendants made it empty",
            )
        )
        schedule(path.parent, order)
    return tuple(selected), tuple(results), tuple(issues)


def purge_space(request: PurgeRequest, *, ui: CleanupUI) -> CleanupReport:
    return cleanup(request, ui=ui)


def clean_files(request: CleanRequest, *, ui: CleanupUI) -> CleanupReport:
    return cleanup(request, ui=ui)


def cleanup(request: PurgeRequest | CleanRequest, *, ui: CleanupUI) -> CleanupReport:
    """Raise CleanupSelectionError for invalid invocations; report config/execution failures."""
    try:
        progress = ui.scan_progress if isinstance(ui, CleanupProgressObserver) else None
        plan = _build_plan(request, progress=progress)
    except CleanupConfigError as exc:
        plan = CleanupPlan(
            (),
            (),
            (CleanupIssue("config_error", request.config, str(exc)),),
            command="purge" if isinstance(request, PurgeRequest) else "clean",
        )
        return _make_report(plan, CleanupDisposition.PARTIAL)
    interactive = ui.is_interactive()

    if not interactive and not request.yes:
        selected = tuple(item for item in plan.candidates if item.choice.preselected)
        results = tuple(
            CleanupItemResult(
                item.choice.exact_path,
                ItemDisposition.WOULD_REMOVE,
                item.choice.size_bytes,
                "non-interactive preview",
            )
            for item in selected
        )
        return _make_report(
            plan, CleanupDisposition.PREVIEW, selected=selected, results=results
        )

    if interactive and isinstance(ui, CleanupPlanPresenter):
        ui.present_plan(plan)

    if not plan.candidates:
        return _make_report(plan, CleanupDisposition.COMPLETED)

    if request.yes:
        selected = plan.candidates
    else:
        selected_ids = ui.select(tuple(item.choice for item in plan.candidates))
        if selected_ids is None or not selected_ids:
            return _make_report(plan, CleanupDisposition.CANCELLED)
        selected, selection_issues = _bind_selection(selected_ids, plan)
        if selection_issues:
            return _make_report(
                plan,
                CleanupDisposition.PARTIAL,
                extra_issues=selection_issues,
            )

    # Stable linear partition: preserve the UI's relative order within each phase.
    purge_selected, other_selected = [], []
    for item in selected:
        (purge_selected if item.choice.purge_phase else other_selected).append(item)
    selected = (*purge_selected, *other_selected)

    if request.dry_run:
        results = tuple(
            CleanupItemResult(
                item.choice.exact_path,
                ItemDisposition.WOULD_REMOVE,
                item.choice.size_bytes,
                "dry-run preview",
            )
            for item in selected
        )
        return _make_report(
            plan, CleanupDisposition.PREVIEW, selected=selected, results=results
        )

    known_bytes = _selection_known_bytes(selected)
    exact_paths = tuple(item.choice.exact_path for item in selected)
    if not request.yes and not ui.confirm_exact(exact_paths, known_bytes):
        return _make_report(plan, CleanupDisposition.CANCELLED, selected=selected)

    results: list[CleanupItemResult] = []
    removed_links: set[tuple[int, int]] = set()
    extra_issues: tuple[CleanupIssue, ...] = ()
    for index, item in enumerate(selected):
        item = replace(item, removed_links=frozenset(removed_links))
        if plan.snapshot is not None and not configuration_unchanged(plan.snapshot):
            reason = "cleanup configuration changed after review; preview again"
            extra_issues = (CleanupIssue("config_changed", None, reason),)
            results.extend(
                CleanupItemResult(
                    rest.choice.exact_path,
                    ItemDisposition.SKIPPED,
                    rest.choice.size_bytes,
                    reason,
                )
                for rest in selected[index:]
            )
            break
        try:
            with scan_budget(), installation_lock(item.choice.exact_path, item.agent):
                reason = _revalidate(item)
                if reason is not None:
                    results.append(
                        CleanupItemResult(
                            item.choice.exact_path,
                            ItemDisposition.SKIPPED,
                            item.choice.size_bytes,
                            reason,
                        )
                    )
                    continue
                # Rechecking a large tree can take time; guard configuration again.
                if plan.snapshot is not None and not configuration_unchanged(
                    plan.snapshot
                ):
                    reason = "cleanup configuration changed during revalidation; preview again"
                    extra_issues = (CleanupIssue("config_changed", None, reason),)
                    results.extend(
                        CleanupItemResult(
                            rest.choice.exact_path,
                            ItemDisposition.SKIPPED,
                            rest.choice.size_bytes,
                            reason,
                        )
                        for rest in selected[index:]
                    )
                    break
                _permanently_remove(item)
        except OSError as exc:
            results.append(
                CleanupItemResult(
                    item.choice.exact_path,
                    ItemDisposition.FAILED,
                    item.choice.size_bytes,
                    str(exc),
                )
            )
        else:
            removed_links.update(
                entry.identity
                for entry in item.hardlink_entries
                if entry.identity is not None
            )
            results.append(
                CleanupItemResult(
                    item.choice.exact_path,
                    ItemDisposition.REMOVED,
                    item.choice.size_bytes,
                )
            )
    cascade_selected: tuple[_BoundCandidate, ...] = ()
    if not extra_issues:
        cascade_selected, cascade_results, cascade_issues = _cascade_empty_directories(
            plan,
            tuple(
                result.exact_path
                for result in results
                if result.disposition is ItemDisposition.REMOVED
            ),
            removed_links,
        )
        results.extend(cascade_results)
        extra_issues = (*extra_issues, *cascade_issues)
    result_tuple = tuple(results)
    return _make_report(
        plan,
        CleanupDisposition.COMPLETED,
        selected=(*selected, *cascade_selected),
        results=result_tuple,
        extra_issues=extra_issues,
    )
