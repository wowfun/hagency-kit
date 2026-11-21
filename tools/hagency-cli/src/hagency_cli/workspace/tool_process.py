"""Invoke package managers with argument boundaries intact."""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress, emit_event


@contextmanager
def external_metadata(source: str):
    try:
        yield
    except (KeyError, TypeError, IndexError, AttributeError, ValueError) as exc:
        fail(f"invalid metadata from {source}: {exc}")


def _stream(command, env, timeout, progress):
    child = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    output = queue.Queue(maxsize=64)
    stopped = threading.Event()

    def enqueue(line):
        while not stopped.is_set():
            try:
                output.put(line, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_output():
        try:
            with child.stdout as stream:
                for line in iter(lambda: stream.readline(8192), ""):
                    if stopped.is_set():
                        break
                    enqueue(line.rstrip("\r\n"))
        finally:
            enqueue(None)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout
    tail = deque(maxlen=12)
    try:
        while True:
            try:
                line = output.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise subprocess.TimeoutExpired(command, timeout) from None
            if line is None:
                break
            emit_event(progress, line, error=True)
            tail.append(line[-2000:])
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
        code = child.wait(timeout=max(0, deadline - time.monotonic()))
    finally:
        stopped.set()
        if child.poll() is None:
            child.kill()
            child.wait()
    return subprocess.CompletedProcess(command, code, "", "\n".join(tail))


def node_script(shim: Path) -> Path | None:
    """Find the JavaScript entry point in npm, Corepack and pnpm Windows shims."""
    if shim.suffix.lower() not in {".cmd", ".bat"}:
        return None
    text = shim.read_text(encoding="utf-8", errors="replace")
    if shim.name.lower() == "npm.cmd" and "NPM_CLI_JS" in text:
        cli = shim.parent / "node_modules/npm/bin/npm-cli.js"
        if cli.is_file():
            return cli
    for ref in re.findall(
        r'"((?:%~dp0|%dp0%)[^"\r\n]+\.(?:[cm]?js))"', text, re.IGNORECASE
    ):
        script = shim.parent / re.sub(
            r"^%(?:~dp0|dp0%)[\\/]*", "", ref, flags=re.IGNORECASE
        )
        if script.is_file() and script.name != "npm-prefix.js":
            return script.resolve()
    return None


def process(
    argv: list[str],
    *,
    env: dict | None = None,
    check: bool = True,
    timeout: int = 600,
    progress: Progress | None = None,
) -> subprocess.CompletedProcess:
    try:
        command = argv
        if os.name == "nt":
            exe = shutil.which(argv[0], path=(env or os.environ).get("PATH"))
            if exe and Path(exe).suffix.lower() in {".cmd", ".bat"}:
                shim = Path(exe)
                command = [str(shim), *argv[1:]]
                script = node_script(shim)
                if script:
                    node = shim.with_name("node.exe")
                    command = [
                        str(node) if node.is_file() else "node",
                        str(script),
                        *argv[1:],
                    ]
                elif any(re.search(r'[&|<>^%!()\r\n"]', arg) for arg in argv[1:]):
                    fail(
                        f"cannot pass shell metacharacters through batch wrapper {shim}"
                    )
        result = (
            _stream(command, env, timeout, progress)
            if progress is not None
            else subprocess.run(
                command,
                check=False,
                env=env,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"cannot run {argv[0]}: {exc}")
    if check and result.returncode:
        fail(
            f"{shlex.join(argv)} exited {result.returncode}: {(result.stderr or result.stdout).strip()}"
        )
    return result


def query_json(argv: list[str], env: dict | None = None):
    output = process(argv, env=env).stdout
    try:
        return json.loads(output)
    except ValueError:
        fail(f"{shlex.join(argv)} returned invalid JSON")
