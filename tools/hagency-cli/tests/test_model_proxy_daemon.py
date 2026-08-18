from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hagency_cli.model_proxy import daemon


class ModelProxyDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "hagency-model-proxy.toml"
        self.config.write_text(
            'version = 1\ndefault_provider = "openai"\n[providers.openai]\n'
            'adapter = "openai"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_paths_follow_linux_and_windows_conventions(self) -> None:
        linux = daemon.service_paths(
            self.config,
            environ={"XDG_STATE_HOME": str(self.root / "xdg")},
            platform="posix",
        )
        windows = daemon.service_paths(
            self.config,
            environ={"LOCALAPPDATA": str(self.root / "local")},
            platform="nt",
        )

        self.assertEqual(linux.directory.parents[1], self.root / "xdg" / "hagency")
        self.assertEqual(windows.directory.parents[1], self.root / "local" / "Hagency")
        self.assertEqual(linux.directory.name, windows.directory.name)

    def test_explicit_state_home_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(
            daemon.ModelProxyServiceError, "HAGENCY_STATE_HOME.*absolute"
        ):
            daemon.service_paths(
                self.config,
                environ={"HAGENCY_STATE_HOME": "relative/state"},
                platform="posix",
            )

    def test_state_rejects_boolean_numeric_fields(self) -> None:
        state_home = self.root / "state"
        with mock.patch.dict(
            os.environ, {"HAGENCY_STATE_HOME": str(state_home)}, clear=False
        ):
            paths = daemon.service_paths(self.config)
            paths.directory.mkdir(parents=True)
            paths.state.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "config": str(self.config),
                        "host": "127.0.0.1",
                        "port": 8765,
                        "started_at": True,
                        "process_identity": "linux:1",
                        "startup_nonce": "attempt-1",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                daemon.ModelProxyServiceError, "invalid model proxy state"
            ):
                daemon.stop_model_proxy(self.config)

    def test_service_log_rotates_at_the_size_limit(self) -> None:
        paths = daemon.service_paths(
            self.config,
            environ={"HAGENCY_STATE_HOME": str(self.root / "state")},
        )
        paths.directory.mkdir(parents=True)
        paths.log.write_bytes(b"new-log")
        paths.log.with_name("service.log.1").write_bytes(b"previous-log")

        daemon._rotate_service_log(paths.log, max_bytes=4, backups=2)

        self.assertFalse(paths.log.exists())
        self.assertEqual(paths.log.with_name("service.log.1").read_bytes(), b"new-log")
        self.assertEqual(
            paths.log.with_name("service.log.2").read_bytes(), b"previous-log"
        )

    def test_spawn_uses_detached_flags_on_windows_and_new_session_on_linux(
        self,
    ) -> None:
        with mock.patch.object(subprocess, "Popen") as popen:
            daemon._spawn_worker(
                ["python", "worker"],
                mock.sentinel.log,
                environ={},
                platform="nt",
            )
            windows_options = popen.call_args.kwargs
            self.assertEqual(
                windows_options["creationflags"],
                daemon.WINDOWS_CREATE_NEW_PROCESS_GROUP
                | daemon.WINDOWS_DETACHED_PROCESS
                | daemon.WINDOWS_CREATE_NO_WINDOW,
            )
            self.assertNotIn("start_new_session", windows_options)

            popen.reset_mock()
            daemon._spawn_worker(
                ["python", "worker"],
                mock.sentinel.log,
                environ={},
                platform="posix",
            )
            linux_options = popen.call_args.kwargs
            self.assertTrue(linux_options["start_new_session"])
            self.assertNotIn("creationflags", linux_options)

    def test_worker_command_preserves_non_cpython_interpreter_environment(self) -> None:
        with (
            mock.patch.object(daemon.sys, "executable", "/opt/venv/bin/pypy3"),
            mock.patch.object(daemon.sys, "frozen", False, create=True),
            mock.patch("shutil.which", return_value="/usr/bin/python") as which,
        ):
            command = daemon._worker_command(self.config, "127.0.0.1", 8765)

        self.assertEqual(command[0], "/opt/venv/bin/pypy3")
        which.assert_not_called()

    def test_start_ignores_delayed_state_from_a_previous_attempt(self) -> None:
        paths = daemon.service_paths(
            self.config,
            environ={"HAGENCY_STATE_HOME": str(self.root / "state")},
        )
        paths.directory.mkdir(parents=True)
        stale = daemon.ServiceState(
            pid=111,
            config=self.config,
            host="127.0.0.1",
            port=8001,
            started_at=1,
            process_identity="linux:stale",
            startup_nonce="old-attempt",
        )
        ready = daemon.ServiceState(
            pid=222,
            config=self.config,
            host="127.0.0.1",
            port=8002,
            started_at=2,
            process_identity="linux:ready",
            startup_nonce="new-attempt",
        )
        process = mock.Mock(pid=222)
        process.poll.return_value = None
        try:
            with (
                mock.patch.object(
                    daemon, "_read_state", side_effect=(None, stale, ready)
                ),
                mock.patch.object(daemon, "_spawn_worker", return_value=process),
                mock.patch.object(daemon, "_worker_command", return_value=["worker"]),
                mock.patch.object(daemon, "_worker_environment", return_value={}),
                mock.patch.object(daemon, "_pid_alive", return_value=True),
                mock.patch.object(
                    daemon, "process_identity", return_value="linux:ready"
                ),
                mock.patch.object(daemon.time, "sleep"),
                mock.patch.object(
                    daemon.secrets, "token_hex", return_value="new-attempt"
                ),
            ):
                state, _returned_paths = daemon._start_model_proxy_locked(
                    self.config,
                    paths,
                    host="127.0.0.1",
                    port=8002,
                    start_timeout_seconds=1,
                )
        finally:
            daemon._LOCAL_PROCESSES.pop(process.pid, None)

        self.assertEqual(state, ready)

    def test_public_lifecycle_rejects_non_loopback_bind_before_spawning(self) -> None:
        with mock.patch.object(daemon, "_spawn_worker") as spawn:
            with self.assertRaisesRegex(ValueError, "loopback IP address"):
                daemon.start_model_proxy(
                    self.config,
                    host="0.0.0.0",
                    port=8765,
                    start_timeout_seconds=0.01,
                )
        spawn.assert_not_called()

    def test_windows_liveness_probe_never_sends_a_signal(self) -> None:
        with (
            mock.patch.object(
                daemon, "_windows_pid_alive", return_value=True, create=True
            ) as windows_probe,
            mock.patch.object(daemon.os, "kill") as kill,
        ):
            self.assertTrue(daemon._pid_alive(12345, platform="nt"))
        windows_probe.assert_called_once_with(12345)
        kill.assert_not_called()

    def test_lifecycle_lock_has_no_empty_claim_window(self) -> None:
        state_home = self.root / "state"
        first_claim_created = threading.Event()
        release_first_claim = threading.Event()
        critical_entered = threading.Event()
        release_critical = threading.Event()
        errors: list[BaseException] = []
        active = 0
        max_active = 0
        active_lock = threading.Lock()
        real_write = daemon.os.write

        def block_first_write(descriptor: int, data: bytes) -> int:
            if not first_claim_created.is_set():
                first_claim_created.set()
                release_first_claim.wait(timeout=5)
            return real_write(descriptor, data)

        def blocking_read(_path: Path):
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            critical_entered.set()
            release_critical.wait(timeout=5)
            with active_lock:
                active -= 1
            return None

        def stop() -> None:
            try:
                daemon.stop_model_proxy(self.config)
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch.dict(
                os.environ, {"HAGENCY_STATE_HOME": str(state_home)}, clear=False
            ),
            mock.patch.object(daemon.os, "write", side_effect=block_first_write),
            mock.patch.object(daemon, "_read_state", side_effect=blocking_read),
        ):
            first = threading.Thread(target=stop)
            first.start()
            self.assertTrue(first_claim_created.wait(timeout=5))
            second = threading.Thread(target=stop)
            second.start()
            self.assertTrue(critical_entered.wait(timeout=5))
            try:
                release_first_claim.set()
                first.join(timeout=5)
            finally:
                release_critical.set()
                second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(max_active, 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], daemon.ModelProxyServiceError)

    @unittest.skipIf(os.name == "nt", "POSIX lifecycle integration test")
    def test_hook_module_is_loaded_once_by_background_worker(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        counter = self.root / "hook-load-count"
        (hooks / "counter.py").write_text(
            (
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "value = int(counter.read_text()) if counter.exists() else 0\n"
                "counter.write_text(str(value + 1))\n"
                "class Hook:\n"
                "    def __init__(self, init):\n"
                "        self.provider = init.provider\n"
            ),
            encoding="utf-8",
        )
        self.config.write_text(
            'version = 1\ndefault_provider = "openai"\n[providers.openai]\n'
            'adapter = "openai"\nhook = "counter.py"\n',
            encoding="utf-8",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        state_home = self.root / "state"
        with mock.patch.dict(
            os.environ, {"HAGENCY_STATE_HOME": str(state_home)}, clear=False
        ):
            _state, _paths = daemon.start_model_proxy(
                self.config, host="127.0.0.1", port=port
            )
            try:
                self.assertEqual(counter.read_text(encoding="utf-8"), "1")
            finally:
                daemon.stop_model_proxy(self.config)

    @unittest.skipIf(os.name == "nt", "POSIX lifecycle integration test")
    def test_stop_still_works_after_config_is_removed(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        state_home = self.root / "state"
        with mock.patch.dict(
            os.environ, {"HAGENCY_STATE_HOME": str(state_home)}, clear=False
        ):
            _state, _paths = daemon.start_model_proxy(
                self.config, host="127.0.0.1", port=port
            )
            self.config.unlink()
            stopped, _paths = daemon.stop_model_proxy(self.config)
        self.assertTrue(stopped)

    @unittest.skipIf(os.name == "nt", "POSIX lifecycle integration test")
    def test_stop_does_not_signal_reused_pid_with_mismatched_identity(self) -> None:
        state_home = self.root / "state"
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with mock.patch.dict(
                os.environ, {"HAGENCY_STATE_HOME": str(state_home)}, clear=False
            ):
                paths = daemon.service_paths(self.config)
                daemon.write_service_state(
                    paths.state,
                    daemon.ServiceState(
                        pid=sleeper.pid,
                        config=self.config,
                        host="127.0.0.1",
                        port=8765,
                        started_at=0,
                        process_identity="stale-instance",
                        startup_nonce="stale-attempt",
                    ),
                )
                stopped, _paths = daemon.stop_model_proxy(
                    self.config, stop_timeout_seconds=0.01
                )
            self.assertFalse(stopped)
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX lifecycle integration test")
    def test_background_process_starts_restarts_and_stops(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        state_home = self.root / "state"
        with mock.patch.dict(
            os.environ, {"HAGENCY_STATE_HOME": str(state_home)}, clear=False
        ):
            first, paths = daemon.start_model_proxy(
                self.config, host="127.0.0.1", port=port
            )
            try:
                self.assertTrue(daemon._pid_alive(first.pid))
                self.assertTrue(paths.state.is_file())
                real_control_lock = daemon._control_lock
                with mock.patch.object(
                    daemon, "_control_lock", wraps=real_control_lock
                ) as lifecycle_lock:
                    second, second_paths = daemon.restart_model_proxy(
                        self.config, host="127.0.0.1", port=port
                    )
                self.assertEqual(lifecycle_lock.call_count, 1)
                self.assertNotEqual(first.pid, second.pid)
                self.assertEqual(paths, second_paths)
                self.assertTrue(daemon._pid_alive(second.pid))
            finally:
                stopped, _paths = daemon.stop_model_proxy(self.config)
            self.assertTrue(stopped)
            self.assertFalse(paths.state.exists())


if __name__ == "__main__":
    unittest.main()
