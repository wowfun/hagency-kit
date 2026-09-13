from __future__ import annotations

import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from hagency_cli.files.cleanup import agents
from hagency_cli.files.cleanup.inspection import (
    _artifact_verification_reason,
    _context_protection_reason,
    _discover_git_contexts,
    _find_project_root,
    _git_tracked_state,
    _has_link_or_reparse_component,
    _is_link_or_reparse,
    _is_mount,
    _is_reparse_stat,
    _is_within,
    _measure_candidate,
    _resolve_path,
    _same_identity,
)
from hagency_cli.files.cleanup.models import (
    Activity,
    _BoundCandidate,
    _Identity,
    _TreeEntry,
)
from hagency_cli.files.cleanup.policy import age_policy, included, inspect_policy


def _revalidate(candidate: _BoundCandidate) -> str | None:
    path = candidate.choice.exact_path
    if not _same_identity(candidate.root, candidate.root_identity):
        return "scan root changed after review"
    if not _same_identity(path.parent, candidate.parent_identity):
        return "parent directory changed after review"
    if not _same_identity(path, candidate.target_identity):
        return "candidate changed after review"
    try:
        if _is_link_or_reparse(path) or not _is_within(
            _resolve_path(path), _resolve_path(candidate.root)
        ):
            return "candidate is no longer a safe filesystem entry"
    except OSError:
        return "candidate can no longer be resolved safely"

    if _has_link_or_reparse_component(path):
        return "candidate has a linked ancestor after review"
    if not all(
        included(scope, path, stat.S_ISDIR(candidate.target_identity.mode))
        for scope in candidate.scopes
    ):
        if any(scope.rule.source == "cachedir-tag" for scope in candidate.scopes):
            return "candidate no longer has a valid CACHEDIR.TAG"
        return "candidate no longer matches its cleanup rules"
    is_project = any(scope.rule.command == "purge" for scope in candidate.scopes)
    if is_project:
        project_root = _find_project_root(path, candidate.root)
        if candidate.choice.project_verified:
            if project_root is None:
                return "candidate no longer has a safe project context"
            if _resolve_path(project_root) != candidate.choice.project_path:
                return "candidate project ownership changed after review"
        else:
            if project_root is not None:
                return (
                    "candidate acquired a project context after review; preview again"
                )
        protection_reason = _context_protection_reason(
            path, project_root or candidate.root
        )
        if protection_reason:
            return protection_reason
        if (
            _artifact_verification_reason(path)
            != candidate.choice.artifact_unverified_reason
        ):
            return "candidate artifact verification changed after review; preview again"
    if candidate.agent:
        reason = agents.process_block_reason(candidate.agent)
        if reason:
            return reason
        source = candidate.scopes[0].rule.source
        if source.endswith(".versions"):
            keep = max(
                scope.rule.keep_previous
                for scope in candidate.exclusions
                if scope.rule.source == source
            )
            inventory = agents.version_inventory(source, keep)
        else:
            inventory = agents.staging_inventory()
        entry = next((entry for entry in inventory if entry.path == path), None)
        if entry is None or entry.reason:
            return entry.reason if entry else "Agent installation changed after review"
        reason = agents.occupancy_block_reason(path)
        if reason:
            return reason

    git_contexts, git_error = _discover_git_contexts(path)
    if git_error is not None:
        return f"Git safety check failed: {git_error}"
    if git_contexts != candidate.git_contexts:
        return "candidate Git repository identity changed after review"
    for git_context in git_contexts:
        tracked_path = (
            path
            if _is_within(path, git_context.root, allow_equal=True)
            else git_context.root
        )
        tracked, git_error = _git_tracked_state(tracked_path, git_context.root)
        if tracked is None:
            return f"Git safety check failed: {git_error}"
        if tracked:
            return "candidate now contains Git-tracked content"

    now_ns = time.time_ns()
    tree: list[_TreeEntry] = []
    _size, activity, measure_error, _entries = _measure_candidate(
        path,
        now_ns,
        inspect_entry=inspect_policy(
            candidate.exclusions, agent=candidate.agent, project=is_project
        ),
        tree=tree,
    )
    if measure_error is None:
        activity, measure_error = age_policy(
            path, tree, candidate.scopes, candidate.exclusions, now_ns
        )
    if measure_error is not None:
        return f"activity safety check failed: {measure_error}"
    if len(tree) != len(candidate.tree) or any(
        not _same_tree_entry(expected, actual, candidate.removed_links)
        for expected, actual in zip(candidate.tree, tree, strict=True)
    ):
        return "candidate contents or metadata changed after review; preview again"
    if candidate.choice.activity is Activity.OLD and activity is not Activity.OLD:
        return "candidate activity changed after review"
    if (
        candidate.choice.activity is not Activity.UNCERTAIN
        and activity is Activity.UNCERTAIN
    ):
        return "candidate activity can no longer be verified"
    return None


def _remove_tree_no_follow(
    path: Path,
    expected_identity: _Identity | None = None,
    *,
    frozen: tuple[_TreeEntry, ...],
    removed_links: set[tuple[int, int]] | None = None,
) -> None:
    removed_links = set() if removed_links is None else removed_links
    manifest = _frozen_manifest(frozen)
    stack: list[tuple[Path, _Identity | None, bool]] = [
        (path, expected_identity, False)
    ]
    while stack:
        current, expected, visited = stack.pop()
        info = current.stat(follow_symlinks=False)
        identity = _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        if (
            not identity.inode
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse_stat(info)
        ):
            raise OSError(f"refusing to recurse into unsafe directory: {current}")
        if expected is not None and identity != expected:
            raise OSError(f"directory identity changed during removal: {current}")
        if visited:
            current.rmdir()
            continue
        _check_frozen(
            current.relative_to(path).as_posix(), info, manifest, removed_links
        )
        if info.st_dev != manifest["."].identity.device or _is_mount(current):
            raise OSError(f"mount point appeared during removal: {current}")

        with os.scandir(current) as entries:
            children = [Path(entry.path) for entry in entries]
        stack.append((current, identity, True))
        for child in reversed(children):
            child_info = child.stat(follow_symlinks=False)
            if child_info.st_dev != manifest["."].identity.device:
                raise OSError(f"cross-device entry appeared during removal: {child}")
            _check_frozen(
                child.relative_to(path).as_posix(), child_info, manifest, removed_links
            )
            child_identity = _Identity(
                child_info.st_dev,
                child_info.st_ino,
                stat.S_IFMT(child_info.st_mode),
            )
            if stat.S_ISDIR(child_info.st_mode) and not _is_reparse_stat(child_info):
                stack.append((child, child_identity, False))
            elif stat.S_ISDIR(child_info.st_mode) and _is_reparse_stat(child_info):
                child.rmdir()
            else:
                child.unlink()
                if child_info.st_nlink > 1:
                    removed_links.add((child_info.st_dev, child_info.st_ino))


@dataclass
class _FdRemovalFrame:
    directory_fd: int
    parent_fd: int | None
    name: str | None
    expected_identity: _Identity
    names: list[str]
    index: int = 0
    owns_fd: bool = True
    relative: str = ""


def _fd_mount_id(fd: int) -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    with Path(f"/proc/self/fdinfo/{fd}").open(encoding="ascii") as stream:
        for line in stream:
            if line.startswith("mnt_id:"):
                try:
                    return int(line.split(":", 1)[1])
                except ValueError as exc:
                    raise OSError("could not verify opened directory mount") from exc
    raise OSError("could not verify opened directory mount")


def _same_tree_entry(
    expected: _TreeEntry,
    actual: _TreeEntry,
    removed_links: set[tuple[int, int]] | frozenset[tuple[int, int]],
) -> bool:
    if (expected.identity.device, expected.identity.inode) in removed_links:
        # Our own unlink/rename changes the ctime of surviving hard links.
        # Path, identity, size and modification time must still match review.
        actual = replace(actual, ctime_ns=expected.ctime_ns)
    return expected == actual


def _check_frozen(
    relative: str,
    info: os.stat_result,
    manifest: dict[str, _TreeEntry],
    removed_links: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
) -> None:
    if not manifest or "." not in manifest:
        raise OSError(
            "deletion requires a frozen manifest including the candidate root"
        )
    actual = _TreeEntry(
        relative,
        _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid if hasattr(os, "geteuid") else None,
    )
    expected = manifest.get(relative)
    if expected is None or not _same_tree_entry(expected, actual, removed_links):
        raise OSError(f"entry changed after review: {relative}")


def _frozen_manifest(frozen: tuple[_TreeEntry, ...]) -> dict[str, _TreeEntry]:
    manifest = {entry.relative_path: entry for entry in frozen}
    if "." not in manifest or len(manifest) != len(frozen):
        raise OSError("deletion requires a complete, nonempty frozen manifest")
    return manifest


def _remove_tree_from_fd(
    directory_fd: int,
    *,
    frozen: tuple[_TreeEntry, ...],
    removed_links: set[tuple[int, int]] | None = None,
) -> None:
    removed_links = set() if removed_links is None else removed_links
    manifest = _frozen_manifest(frozen)
    root_info = os.fstat(directory_fd)
    root_mount = _fd_mount_id(directory_fd)
    _check_frozen(".", root_info, manifest)
    frames = [
        _FdRemovalFrame(
            directory_fd=directory_fd,
            parent_fd=None,
            name=None,
            expected_identity=_Identity(
                root_info.st_dev,
                root_info.st_ino,
                stat.S_IFMT(root_info.st_mode),
            ),
            names=os.listdir(directory_fd),
            owns_fd=False,
        )
    ]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        while frames:
            frame = frames[-1]
            if frame.index >= len(frame.names):
                try:
                    if frame.parent_fd is not None and frame.name is not None:
                        final_info = os.stat(
                            frame.name,
                            dir_fd=frame.parent_fd,
                            follow_symlinks=False,
                        )
                        final_identity = _Identity(
                            final_info.st_dev,
                            final_info.st_ino,
                            stat.S_IFMT(final_info.st_mode),
                        )
                        if final_identity != frame.expected_identity:
                            raise OSError(
                                "directory identity changed during removal: "
                                f"{frame.name}"
                            )
                        os.rmdir(frame.name, dir_fd=frame.parent_fd)
                finally:
                    if frame.owns_fd:
                        os.close(frame.directory_fd)
                        frame.owns_fd = False
                frames.pop()
                continue

            name = frame.names[frame.index]
            frame.index += 1
            info = os.stat(name, dir_fd=frame.directory_fd, follow_symlinks=False)
            relative = f"{frame.relative}/{name}" if frame.relative else name
            _check_frozen(relative, info, manifest, removed_links)
            if stat.S_ISDIR(info.st_mode) and not _is_reparse_stat(info):
                if info.st_dev != root_info.st_dev or _is_mount(
                    f"/proc/self/fd/{frame.directory_fd}/{name}"
                ):
                    raise OSError(f"mount point appeared during removal: {relative}")
                child_fd = os.open(name, flags, dir_fd=frame.directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if _fd_mount_id(child_fd) != root_mount:
                        raise OSError(f"mount point changed during removal: {relative}")
                    expected = _Identity(
                        info.st_dev,
                        info.st_ino,
                        stat.S_IFMT(info.st_mode),
                    )
                    actual = _Identity(
                        opened.st_dev,
                        opened.st_ino,
                        stat.S_IFMT(opened.st_mode),
                    )
                    if not actual.inode or actual != expected:
                        raise OSError(
                            f"directory identity changed during removal: {name}"
                        )
                    names = os.listdir(child_fd)
                except BaseException:
                    os.close(child_fd)
                    raise
                frames.append(
                    _FdRemovalFrame(
                        directory_fd=child_fd,
                        parent_fd=frame.directory_fd,
                        name=name,
                        expected_identity=expected,
                        names=names,
                        relative=relative,
                    )
                )
            elif stat.S_ISDIR(info.st_mode) and _is_reparse_stat(info):
                os.rmdir(name, dir_fd=frame.directory_fd)
            else:
                os.unlink(name, dir_fd=frame.directory_fd)
                if info.st_nlink > 1:
                    removed_links.add((info.st_dev, info.st_ino))
    finally:
        for frame in frames:
            if frame.owns_fd:
                os.close(frame.directory_fd)
                frame.owns_fd = False


def _permanently_remove(candidate: _BoundCandidate) -> None:
    path = candidate.choice.exact_path
    manifest = _frozen_manifest(candidate.tree)
    removed_links = set(candidate.removed_links)
    has_fd_removal = os.listdir in os.supports_fd and all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.unlink, os.rmdir, os.rename)
    )
    if os.name == "nt" or not has_fd_removal:
        if not _same_identity(path.parent, candidate.parent_identity):
            raise OSError("parent identity changed immediately before removal")
        if not _same_identity(path, candidate.target_identity):
            raise OSError("candidate identity changed immediately before removal")
        _check_frozen(".", path.lstat(), manifest, removed_links)
        quarantine = path.parent / f".hagency-cleanup-{uuid.uuid4().hex}"
        os.replace(path, quarantine)
        try:
            if not _same_identity(quarantine, candidate.target_identity):
                raise OSError("candidate identity changed before atomic quarantine")
            # Our rename changes only ctime. Keep the root in the manifest and
            # check its identity, ownership, size and mtime after the claim.
            claimed = quarantine.lstat()
            manifest["."] = replace(manifest["."], ctime_ns=claimed.st_ctime_ns)
            _check_frozen(".", claimed, manifest, removed_links)
            if stat.S_ISREG(candidate.target_identity.mode):
                quarantine.unlink()
            else:
                _remove_tree_no_follow(
                    quarantine,
                    candidate.target_identity,
                    frozen=tuple(manifest.values()),
                    removed_links=removed_links,
                )
        except BaseException as exc:
            recovery = ""
            if os.path.lexists(quarantine) and not os.path.lexists(path):
                try:
                    os.replace(quarantine, path)
                except OSError:
                    recovery = f"; remaining data is at {quarantine}"
            elif os.path.lexists(quarantine):
                recovery = f"; remaining data is at {quarantine}"
            if isinstance(exc, OSError):
                raise OSError(f"{exc}{recovery}") from exc
            if recovery and hasattr(exc, "add_note"):
                exc.add_note(recovery.removeprefix("; "))
            raise
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, flags)
    try:
        parent_info = os.fstat(parent_fd)
        if (
            _Identity(
                parent_info.st_dev,
                parent_info.st_ino,
                stat.S_IFMT(parent_info.st_mode),
            )
            != candidate.parent_identity
        ):
            raise OSError("parent identity changed immediately before removal")
        target_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _Identity(
                target_info.st_dev,
                target_info.st_ino,
                stat.S_IFMT(target_info.st_mode),
            )
            != candidate.target_identity
        ):
            raise OSError("candidate identity changed immediately before removal")
        if stat.S_ISREG(target_info.st_mode):
            _check_frozen(
                ".",
                target_info,
                manifest,
                removed_links,
            )
            quarantine_name = f".hagency-cleanup-{uuid.uuid4().hex}"
            os.rename(
                path.name, quarantine_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            final_info = os.stat(
                quarantine_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                _Identity(
                    final_info.st_dev,
                    final_info.st_ino,
                    stat.S_IFMT(final_info.st_mode),
                )
                != candidate.target_identity
            ):
                raise OSError(
                    f"file changed before removal; inspect remaining data at {path.with_name(quarantine_name)}"
                )
            manifest["."] = replace(manifest["."], ctime_ns=final_info.st_ctime_ns)
            try:
                _check_frozen(".", final_info, manifest, removed_links)
            except OSError as exc:
                raise OSError(
                    f"{exc}; inspect remaining data at {path.with_name(quarantine_name)}"
                ) from exc
            os.unlink(quarantine_name, dir_fd=parent_fd)
            return
        if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
            raise OSError("candidate is no longer a real directory")

        target_fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            if _fd_mount_id(target_fd) != _fd_mount_id(parent_fd):
                raise OSError("candidate became a mount point during removal")
            opened = os.fstat(target_fd)
            if (opened.st_dev, opened.st_ino) != (
                target_info.st_dev,
                target_info.st_ino,
            ):
                raise OSError("candidate identity changed during removal")
            _remove_tree_from_fd(
                target_fd, frozen=candidate.tree, removed_links=removed_links
            )
            # Claim a private name before the final unlink. Concurrent builds
            # may recreate the original artifact path after it becomes empty;
            # rmdir must never remove that newly created directory instead.
            quarantine_name = f".hagency-cleanup-{uuid.uuid4().hex}"
            os.rename(
                path.name,
                quarantine_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            quarantine = path.with_name(quarantine_name)
            try:
                final_info = os.stat(
                    quarantine_name, dir_fd=parent_fd, follow_symlinks=False
                )
                if (final_info.st_dev, final_info.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise OSError("candidate identity changed before final removal")
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except BaseException as exc:
                # Retain failed quarantine contents rather than restoring over
                # a concurrently recreated artifact. Report their recovery path.
                recovery = f"; inspect remaining data at {quarantine}"
                if isinstance(exc, OSError):
                    raise OSError(f"{exc}{recovery}") from exc
                if hasattr(exc, "add_note"):
                    exc.add_note(recovery.removeprefix("; "))
                raise
        finally:
            os.close(target_fd)
    finally:
        os.close(parent_fd)
