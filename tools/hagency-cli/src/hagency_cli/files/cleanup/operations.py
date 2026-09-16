from __future__ import annotations

from dataclasses import replace

from hagency_cli.files.cleanup.agents import installation_lock
from hagency_cli.files.cleanup.config import CleanupConfigError, configuration_unchanged
from hagency_cli.files.cleanup.inspection import scan_budget
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
from hagency_cli.files.cleanup.removal import _permanently_remove, _revalidate
from hagency_cli.files.cleanup.scan import _build_plan


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
    result_tuple = tuple(results)
    return _make_report(
        plan,
        CleanupDisposition.COMPLETED,
        selected=selected,
        results=result_tuple,
        extra_issues=extra_issues,
    )
