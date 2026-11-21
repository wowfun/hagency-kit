"""Installation receipts and current-user command exposure."""

from __future__ import annotations

import ctypes
import errno
import getpass
import hashlib
import os
import sqlite3
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from hagency_cli.paths import user_state_dir
from hagency_cli.workspace.errors import fail


@dataclass(frozen=True)
class Receipt:
    name: str
    root: str
    method: str
    package: str
    path: str
    checksum: str | None


@contextmanager
def tool_lock(*identities: str):
    """Serialize overlapping catalog names, packages and commands across processes."""
    directory = user_state_dir() / "tools" / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as files:
        for key in sorted({name.casefold() for name in identities}):
            path = directory / hashlib.sha256(key.encode()).hexdigest()
            handle = files.enter_context(path.open("a+b"))
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    fail(
                        f"another tool operation is running for {key}; retry after it finishes"
                    )
                raise
        yield


@contextmanager
def receipts(write: bool):
    file = user_state_dir() / "tools" / "installs.sqlite3"
    if not write and not file.exists():
        yield None
        return
    if write:
        file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(
        file.as_uri() + ("?mode=rwc" if write else "?mode=ro"), uri=True, timeout=60
    )
    try:
        if write:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS installs (name, root, method, package, path, checksum, PRIMARY KEY(name, root))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS published_dirs (path TEXT PRIMARY KEY)"
            )
        yield db
        if write:
            db.commit()
    finally:
        db.close()


def lookup(db, name: str) -> list[Receipt]:
    # A concurrent first writer may have created the file but not committed its schema yet.
    if db is None or not db.execute("PRAGMA table_info(installs)").fetchone():
        return []
    return [
        Receipt(*row)
        for row in db.execute(
            "SELECT name, root, method, package, path, checksum FROM installs WHERE name=?",
            (name,),
        )
    ]


def remember(db, receipt: Receipt) -> None:
    db.execute(
        "INSERT OR REPLACE INTO installs (name, root, method, package, path, checksum) "
        "VALUES (:name, :root, :method, :package, :path, :checksum)",
        vars(receipt),
    )


def publish(db, directory: Path) -> None:
    if expose_windows(directory):
        db.execute(
            "INSERT OR IGNORE INTO published_dirs (path) VALUES (?)", (str(directory),)
        )


def windows_root(dry_run: bool) -> Path:
    default = Path("D:/hagency") / getpass.getuser() / "tools"
    if default.anchor and Path(default.anchor).is_dir():
        try:
            if dry_run:
                nearest = next(p for p in (default, *default.parents) if p.exists())
                if nearest.is_dir() and os.access(nearest, os.W_OK):
                    return default
            else:
                default.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=default):
                    return default
        except OSError:
            pass
    data = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data.is_absolute():
        data = Path.home() / ".local" / "share"
    return data / "hagency" / "tools"


def expose_windows(directory: Path, *, remove: bool = False) -> bool:
    import winreg

    def normalized(value: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.expandvars(value.strip('"'))))

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        access=winreg.KEY_READ | winreg.KEY_SET_VALUE,
    ) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        parts = value.split(";") if value else []
        remaining = [
            part for part in parts if normalized(part) != normalized(str(directory))
        ]
        if remove:
            if remaining == parts:
                return False
            parts = remaining
        else:
            if remaining != parts:
                return False
            if ";" in str(directory):
                fail(
                    f"cannot add directory containing ';' to Windows PATH: {directory}"
                )
            parts.insert(0, str(directory))
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 2, 1000, None
    )
    return True


def release_receipt(db, receipt: Receipt, *, forget: bool = False) -> None:
    db.execute(
        "DELETE FROM installs WHERE name=? AND root=?", (receipt.name, receipt.root)
    )
    directory = Path(receipt.path).parent
    if (
        os.name != "nt"
        or not db.execute(
            "SELECT 1 FROM published_dirs WHERE path=?", (str(directory),)
        ).fetchone()
    ):
        return
    if any(
        Path(row[0]).parent == directory
        for row in db.execute("SELECT path FROM installs")
    ):
        return
    if not forget:
        extensions = {".exe", ".com", ".cmd", ".bat", ".ps1"}
        if directory.is_dir() and any(
            p.suffix.lower() in extensions and p.is_file() for p in directory.iterdir()
        ):
            return
        expose_windows(directory, remove=True)
    db.execute("DELETE FROM published_dirs WHERE path=?", (str(directory),))
