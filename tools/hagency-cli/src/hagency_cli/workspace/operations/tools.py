from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from packaging.utils import canonicalize_name

from hagency_cli.paths import expand_path
from hagency_cli.workspace import ffmpeg_portable, tool_native, tool_packages
from hagency_cli.workspace.errors import WorkspaceError, fail
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.tool_catalog import (
    LANGUAGE_METHODS,
    METHODS,
    Status,
    Tool,
    probe,
    upstream_version,
)
from hagency_cli.workspace.tool_state import (
    Receipt,
    lookup,
    publish,
    receipts,
    release_receipt,
    remember,
    tool_lock,
    windows_root,
)


@dataclass(frozen=True)
class Target:
    method: str
    root: Path | None
    entry: Path | None

    @property
    def location(self) -> str:
        return str(self.root or self.entry or "manager global directory")


def owned(tool: Tool, method: str, root: Path | None) -> Path | None:
    if method in LANGUAGE_METHODS:
        entry = tool_packages.owned_entry(tool, method, root)
    else:
        entry = tool_native.owned_entry(tool, method)
    return (
        entry if entry and (not root or entry.absolute().is_relative_to(root)) else None
    )


def same_entry(left: Path | None, right: Path | None) -> bool:
    return bool(
        left and right and left.is_file() and right.is_file() and left.samefile(right)
    )


def active_path(tool: Tool) -> Path | None:
    found = shutil.which(tool.command)
    return Path(found).absolute() if found else None


def choose_receipt(
    name: str, explicit: Path | None, active: Path | None = None
) -> Receipt | None:
    with receipts(write=False) as db:
        records = lookup(db, name)
    if explicit is not None:
        return next((r for r in records if r.root and Path(r.root) == explicit), None)
    for record in records:
        if same_entry(Path(record.path), active):
            return record
    if len(records) > 1:
        fail("multiple recorded tool locations; select one with --install-dir")
    return next(iter(records), None)


def forget_installation(
    name: str, install_dir: str | None, dry_run: bool, progress: Progress | None
) -> None:
    try:
        explicit = (
            expand_path(install_dir, Path.cwd()).resolve() if install_dir else None
        )
        with nullcontext() if dry_run else tool_lock(name):
            receipt = choose_receipt(name, explicit)
            if not receipt:
                fail(
                    f"no recorded installation for {name} at {explicit or 'the selected location'}"
                )
            if not dry_run:
                with receipts(write=True) as db:
                    release_receipt(db, receipt, forget=True)
            emit_event(
                progress,
                f"{'Would forget' if dry_run else 'Forgot'} {name} at {receipt.path}",
            )
    except (OSError, sqlite3.Error) as exc:
        raise WorkspaceError(f"{name}: {exc}") from exc


def portable_target(
    tool: Tool, root: Path | None, dry_run: bool, reason: str
) -> Target:
    if not ffmpeg_portable.supports(tool):
        fail(reason)
    if root is None:
        root = windows_root(dry_run) if os.name == "nt" else Path.home() / ".local"
    return Target("portable", root, None)


def recorded_target(tool: Tool, receipt: Receipt, action: str) -> Target:
    if receipt.method not in METHODS | {"portable"}:
        fail(
            f"unknown recorded installer {receipt.method!r}; use hgc tool uninstall {tool.name} --forget"
        )
    if receipt.package.partition("[")[0] != tool.package.partition("[")[0]:
        fail(
            f"recorded package {receipt.package} differs from configured package {tool.package}; "
            f"use hgc tool uninstall {tool.name} --forget to discard the old receipt"
        )
    root = Path(receipt.root) if receipt.root else None
    if receipt.method == "portable":
        entry = Path(receipt.path)
        if (
            not root
            or entry != ffmpeg_portable.destination(root)
            or not ffmpeg_portable.supports(tool)
        ):
            fail(
                f"portable installation no longer matches the declaration; use hgc tool uninstall {tool.name} --forget"
            )
        if action != "uninstall":
            ffmpeg_portable.verify_destination(entry, receipt.checksum)
    else:
        entry = owned(tool, receipt.method, root)
        if not entry and Path(receipt.path).exists():
            fail(
                f"{tool.name}: no installation owned by {receipt.method} at {receipt.path}; "
                f"use hgc tool uninstall {tool.name} --forget to discard the receipt"
            )
        if (
            entry
            and not same_entry(entry, Path(receipt.path))
            and (not root or not entry.resolve().is_relative_to(root))
        ):
            fail(f"{tool.name}: recorded executable changed to {entry}")
    return Target(receipt.method, root, entry)


def resolve_target(
    tool: Tool,
    action: str,
    explicit: Path | None,
    active: Path | None,
    receipt: Receipt | None,
    dry_run: bool,
) -> Target:
    if receipt:
        return recorded_target(tool, receipt, action)

    methods = [m for m in tool.methods if shutil.which("apt-get" if m == "apt" else m)]

    @cache
    def discover(method, root):
        return owned(tool, method, root)

    if active and not explicit:
        for method in methods:
            entry = discover(method, None)
            if same_entry(entry, active):
                return Target(method, None, entry)
    root = explicit
    if root is None and action == "install" and os.name == "nt":
        root = windows_root(dry_run)
    if not methods:
        return portable_target(
            tool,
            root,
            dry_run,
            f"{tool.name}: no available installer among {', '.join(tool.methods)}",
        )
    method = methods[0]
    if method in LANGUAGE_METHODS:
        entry = discover(method, root) if root else None
    else:
        native = discover(method, None)
        entry = (
            native
            if native and (not root or native.absolute().is_relative_to(root))
            else None
        )
        if root and not entry and (method != "winget" or native):
            return portable_target(
                tool, root, dry_run, f"{method} cannot install {tool.package} in {root}"
            )
        if (
            not root
            and not same_entry(entry, active)
            and action in {"uninstall", "upgrade"}
        ):
            entry = None
    return Target(method, root, entry)


def record_install(
    tool: Tool, target: Target, installed: Path, checksum: str | None
) -> None:
    record = Receipt(
        tool.name,
        str(target.root or ""),
        target.method,
        tool.package,
        str(installed),
        checksum,
    )
    with receipts(write=True) as db:
        remember(db, record)
        if os.name == "nt" and target.root:
            try:
                publish(db, installed.parent)
            except (OSError, WorkspaceError):
                # A verified installation can retry PATH publication on its next install.
                db.commit()
                raise


def report_active(
    tool: Tool,
    progress: Progress | None,
    verified: Status | None = None,
    *,
    reused: bool = False,
) -> None:
    path = active_path(tool)
    matches = verified is not None and same_entry(verified.path, path)
    if verified is not None:
        if matches and reused:
            emit_event(progress, verified.display(tool.name))
            return
        emit_event(progress, "Installed: " + verified.display(tool.name))
    if matches:
        status = Status(path, verified.version, verified.status)
    else:
        status = probe(tool, path) if path else Status(None, None, "missing")
    emit_event(progress, "PATH: " + status.display(tool.name))
    if verified and verified.path and not matches:
        emit_event(
            progress,
            f"To select this installation, prepend {verified.path.parent} to the current shell's PATH.",
        )


def execute_change(
    tool: Tool,
    target: Target,
    action: str,
    args: tuple[str, ...],
    version: str | None,
    status: Status,
    receipt: Receipt | None,
    progress: Progress | None,
) -> tuple[Path | None, str | None]:
    method = target.method
    if method == "portable":
        if args:
            fail("the portable FFmpeg downloader has no package-manager arguments")
        path = ffmpeg_portable.destination(target.root)
        checksum = ffmpeg_portable.change_portable(
            tool, path, receipt.checksum if receipt else None, action == "uninstall"
        )
        return path if path.is_file() else None, checksum
    if method in LANGUAGE_METHODS:
        tool_packages.change_package(
            tool, method, target.root, action, args, status.version, progress=progress
        )
    else:
        tool_native.change_native(
            tool,
            method,
            target.root,
            action,
            version,
            args,
            installed=bool(target.entry),
            progress=progress,
        )
    return owned(tool, method, target.root), None


def manage_tool(
    tool: Tool,
    action: str,
    *,
    install_dir: str | None = None,
    extra: tuple[str, ...] = (),
    dry_run: bool = False,
    progress: Progress | None = None,
) -> None:
    try:
        package = tool.package.partition("[")[0]
        lock = (
            tool_lock(tool.name, tool.command, canonicalize_name(package))
            if not dry_run
            else nullcontext()
        )
        with lock:
            explicit = (
                expand_path(install_dir, Path.cwd()).resolve() if install_dir else None
            )
            active = active_path(tool)
            receipt = choose_receipt(tool.name, explicit, active)
            args = (*tool.install_args, *extra) if action != "uninstall" else ()
            if (
                action == "install"
                and active
                and not (receipt or explicit or args or tool.extras)
            ):
                current = probe(tool, active)
                if current.status == "available":
                    emit_event(progress, current.display(tool.name))
                    return
            else:
                current = None
            target = resolve_target(tool, action, explicit, active, receipt, dry_run)
            _apply(tool, target, receipt, action, args, current, dry_run, progress)
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise WorkspaceError(f"{tool.name}: {exc}") from exc


def _apply(
    tool: Tool,
    target: Target,
    receipt: Receipt | None,
    action: str,
    args: tuple[str, ...],
    current: Status | None,
    dry_run: bool,
    progress: Progress | None,
) -> None:
    entry = target.entry
    if action in {"uninstall", "upgrade"} and (not entry or not entry.is_file()):
        if action == "uninstall" and receipt and not Path(receipt.path).exists():
            if not dry_run:
                with receipts(write=True) as db:
                    release_receipt(db, receipt)
            emit_event(progress, f"{tool.name}: already removed from {receipt.path}")
            report_active(tool, progress)
            return
        hint = (
            f"; use hgc tool uninstall {tool.name} --forget to discard the receipt"
            if receipt
            else ""
        )
        fail(
            f"{tool.name}: no installation owned by {target.method} at {target.location}{hint}"
        )

    status = Status(None, None, "missing")
    if entry and action != "uninstall":
        status = (
            current
            if current and same_entry(current.path, entry)
            else probe(tool, entry)
        )
    if (
        action == "install"
        and status.status == "available"
        and not args
        and (
            not tool.extras
            or (
                target.method == "uv" and tool_packages.uv_has_extras(tool, target.root)
            )
        )
    ):
        if not dry_run and os.name == "nt" and target.root:
            record_install(tool, target, entry, receipt.checksum if receipt else None)
        report_active(tool, progress, status, reused=True)
        return

    version = None
    if (
        not dry_run
        and action != "uninstall"
        and target.method not in LANGUAGE_METHODS | {"portable"}
    ):
        version = tool_native.compatible(tool, target.method)
        if version is None:
            reason = f"{target.method}: no compatible {tool.package} version for {tool.version or '*'}"
            if receipt or action == "upgrade":
                fail(reason)
            target = portable_target(tool, target.root, dry_run, reason)
        elif (
            target.method in {"winget", "brew"}
            and not args
            and status.version == upstream_version(version)
        ):
            report_active(tool, progress, status, reused=True)
            return
    emit_event(
        progress,
        f"{'Would ' if dry_run else ''}{action} {tool.package} {tool.version or '*'} via {target.method} at {target.location}"
        + (" " + shlex.join(args) if args else ""),
    )
    if dry_run:
        return

    installed, checksum = execute_change(
        tool, target, action, args, version, status, receipt, progress
    )
    finish_change(tool, target, action, installed, checksum, receipt, progress)


def finish_change(
    tool: Tool,
    target: Target,
    action: str,
    installed: Path | None,
    checksum: str | None,
    receipt: Receipt | None,
    progress: Progress | None,
) -> None:
    if action == "uninstall":
        if installed:
            fail(
                f"{target.method} still reports an installed executable at {installed}"
            )
        if receipt:
            with receipts(write=True) as db:
                release_receipt(db, receipt)
        report_active(tool, progress)
        return
    if not installed:
        fail(
            f"{target.method} did not expose {tool.command} at the selected location: {target.location}"
        )
    verified = probe(tool, installed)
    if verified.status != "available":
        fail(f"installed tool failed verification: {verified.display(tool.name)}")
    record_install(tool, target, installed, checksum)
    report_active(tool, progress, verified)
