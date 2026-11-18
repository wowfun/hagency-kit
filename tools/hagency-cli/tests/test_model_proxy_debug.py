from __future__ import annotations

import asyncio
import base64
import errno
import gzip
import json
import logging
import os
import socket
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from multidict import CIMultiDict

from hagency_cli.model_proxy import ModelProxyConfigError, daemon, load_proxy_config
from hagency_cli.model_proxy.debug import (
    CHUNK_BYTES,
    LOGS_KEY,
    TRACE_KEY,
    DebugLogs,
    RequestTrace,
    SafeAccessLogger,
    debug_middleware,
    redact_url,
    request_trace,
)
from hagency_cli.model_proxy.server import (
    _stream_response,
    _upstream_chunks,
    create_model_proxy_app,
)
from tests import test_model_proxy as proxy_tests


def records(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in directory.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def body(events: list[dict], stage: str) -> bytes:
    return b"".join(
        event["body"].encode("utf-8")
        if event["encoding"] == "utf-8"
        else base64.b64decode(event["body"])
        for event in events
        if event["stage"] == stage and event["event"] == "body"
    )


class DebugLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_crash_releases_waiting_emits_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"], queue_size=1)

            async def exercise():
                await logs.emit("corp", {"body": object()})
                await asyncio.gather(
                    *(logs.emit("corp", {"body": "private"}) for _ in range(5)),
                    logs.close(),
                )
                await logs.flush()

            try:
                with self.assertLogs("hagency.model_proxy", level="ERROR") as captured:
                    await asyncio.wait_for(exercise(), 2)
                self.assertEqual(len(captured.output), 1)
                self.assertIn("TypeError", captured.output[0])
                self.assertNotIn("private", captured.output[0])
                self.assertNotIn("not JSON serializable", captured.output[0])
                self.assertTrue(
                    all(handler.stream is None for handler in logs.handlers.values())
                )
            finally:
                if logs._writer is not None:
                    logs._writer.cancel()
                    await asyncio.gather(logs._writer, return_exceptions=True)
                logs._close_handlers()

    def test_debug_directory_error_reports_errno_without_path(self):
        with (
            mock.patch.object(
                Path,
                "mkdir",
                side_effect=PermissionError(errno.EACCES, "private", "secret-path"),
            ),
            self.assertRaisesRegex(RuntimeError, os.strerror(errno.EACCES)) as captured,
        ):
            DebugLogs(Path("secret-path"), ["corp"])
        self.assertNotIn("secret-path", str(captured.exception))
        self.assertNotIn("private", str(captured.exception))

    async def test_upstream_read_failures_have_upstream_reason(self):
        async def interrupted(error):
            yield b"partial"
            raise error

        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"])
            try:
                for error in (ConnectionResetError(), aiohttp.ClientPayloadError()):
                    with self.subTest(error=type(error).__name__):
                        trace = RequestTrace(logs, "corp")
                        await trace.begin("upstream_response")
                        upstream = mock.Mock()
                        upstream.content.iter_any.return_value = interrupted(error)
                        chunks = _upstream_chunks(upstream, trace)
                        self.assertEqual(await anext(chunks), b"partial")
                        with self.assertRaises(aiohttp.ClientConnectionError):
                            await anext(chunks)
                        self.assertEqual(trace.reason, "upstream_connection_error")
                await logs.flush()
                ends = [
                    record for record in records(Path(tmp)) if record["event"] == "end"
                ]
                self.assertEqual(len(ends), 2)
                self.assertTrue(all(not record["complete"] for record in ends))
                self.assertEqual({record["bytes"] for record in ends}, {7})
                self.assertEqual(
                    {record["reason"] for record in ends}, {"upstream_connection_error"}
                )
            finally:
                await logs.close()

    async def test_downstream_write_reset_has_client_reason(self):
        async def chunks():
            yield b"chunk"

        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"])
            try:
                for iterator in (None, chunks()):
                    request = make_mocked_request("GET", "/v1/responses")
                    trace = RequestTrace(logs, "corp")
                    request[TRACE_KEY] = trace
                    await trace.begin("upstream_response")
                    await trace.begin("client_response")
                    upstream = mock.Mock()
                    upstream.content.iter_any.return_value = chunks()
                    with (
                        mock.patch.object(web.StreamResponse, "prepare"),
                        mock.patch.object(
                            web.StreamResponse,
                            "write",
                            side_effect=aiohttp.ClientConnectionResetError(),
                        ),
                        mock.patch.object(web.StreamResponse, "write_eof"),
                    ):
                        await _stream_response(
                            request,
                            upstream,
                            status=200,
                            headers=CIMultiDict(),
                            iterator=iterator,
                            downstream_protocol=proxy_tests.PROTOCOL_RESPONSES,
                            converted=iterator is not None,
                        )
                    upstream.close.assert_called_once()
                    self.assertEqual(trace.reason, "client_disconnected")
            finally:
                await logs.close()

    async def test_slow_storage_does_not_block_loop_and_queue_applies_backpressure(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"], queue_size=2)
            started = threading.Event()
            release = threading.Event()
            writer_threads = []
            original = logs._write_batch

            def stalled_write(batch):
                writer_threads.append(threading.get_ident())
                started.set()
                if not release.wait(timeout=5):
                    raise AssertionError("event loop did not release writer")
                original(batch)

            with mock.patch.object(logs, "_write_batch", side_effect=stalled_write):
                try:
                    await logs.emit("corp", {"index": 0})
                    self.assertTrue(
                        await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
                    )
                    self.assertNotEqual(writer_threads[0], threading.get_ident())
                    await logs.emit("corp", {"index": 1})
                    await logs.emit("corp", {"index": 2})
                    pending = asyncio.create_task(logs.emit("corp", {"index": 3}))
                    # A callback still runs even though the writer and producer are blocked.
                    heartbeat = asyncio.get_running_loop().create_future()
                    asyncio.get_running_loop().call_soon(heartbeat.set_result, None)
                    await heartbeat
                    self.assertFalse(pending.done())
                    self.assertEqual(logs._queue.qsize(), 2)
                    release.set()
                    await asyncio.wait_for(pending, 2)
                    await asyncio.wait_for(logs.close(), 2)
                    self.assertEqual(
                        [event["index"] for event in records(Path(tmp))], [0, 1, 2, 3]
                    )
                    self.assertTrue(logs._writer.done())
                    self.assertTrue(
                        all(
                            handler.stream is None for handler in logs.handlers.values()
                        )
                    )
                finally:
                    release.set()
                    await logs.close()

    async def test_writer_failure_isolated_to_provider_and_close_drains(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp", "other"])
            try:
                with (
                    mock.patch.object(
                        logs.handlers["corp"],
                        "shouldRollover",
                        side_effect=OSError("private"),
                    ),
                    self.assertLogs("hagency.model_proxy", level="ERROR") as captured,
                ):
                    for index in range(10):
                        await logs.emit("corp", {"index": index})
                        await logs.emit("other", {"index": index})
                    await logs.close()
                self.assertEqual(len(captured.output), 1)
                self.assertNotIn("private", str(captured.output))
                self.assertEqual(
                    [event["index"] for event in records(Path(tmp))], list(range(10))
                )
            finally:
                await logs.close()

    async def test_cancellation_while_queue_is_full_retains_incomplete_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"], queue_size=1)
            started = threading.Event()
            release = threading.Event()
            original = logs._write_batch

            def stalled_write(batch):
                started.set()
                if not release.wait(timeout=5):
                    raise AssertionError("writer was not released")
                original(batch)

            with mock.patch.object(logs, "_write_batch", side_effect=stalled_write):
                try:
                    trace = RequestTrace(logs, "corp")
                    await trace.begin("upstream_response", status=200)
                    self.assertTrue(
                        await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
                    )
                    await trace.body("upstream_response", b"partial")
                    pending = asyncio.create_task(trace.end("upstream_response"))
                    heartbeat = asyncio.get_running_loop().create_future()
                    asyncio.get_running_loop().call_soon(heartbeat.set_result, None)
                    await heartbeat
                    self.assertFalse(pending.done())
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                    trace.reason = "cancelled"
                    release.set()
                    await trace.finish()
                    await logs.flush()
                    events = [
                        event
                        for event in records(Path(tmp))
                        if event["stage"] == "upstream_response"
                    ]
                    self.assertEqual(events[-1]["event"], "end")
                    self.assertFalse(events[-1]["complete"])
                    self.assertEqual(events[-1]["reason"], "cancelled")
                finally:
                    release.set()
                    await logs.close()

    async def test_lossless_bodies_and_provider_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            logs = DebugLogs(directory, ["con", "Corp", "corp"])
            try:
                raw = b"a" * (CHUNK_BYTES - 1) + "中文".encode() + b"\xff\x00"
                for name in ("con", "Corp", "corp", "../../unknown"):
                    trace = RequestTrace(logs, name)
                    await trace.begin("client_request")
                    await trace.body("client_request", raw)
                    await trace.end("client_request")
                    await trace.finish()
                await logs.flush()
                self.assertEqual(len(list(directory.glob("*.jsonl"))), 4)
                for path in directory.glob("*.jsonl"):
                    events = [
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8").splitlines()
                    ]
                    self.assertEqual(body(events, "client_request"), raw)
                    self.assertTrue(
                        all(event.get("size", 0) <= CHUNK_BYTES for event in events)
                    )
            finally:
                await logs.close()
            self.assertTrue(
                all(handler.stream is None for handler in logs.handlers.values())
            )

    async def test_rotation_happens_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"], max_bytes=300, backups=3)
            try:
                for index in range(20):
                    await logs.emit("corp", {"index": index, "body": "x" * 200})
                await logs.flush()
                files = sorted(Path(tmp).glob("provider-*"))
                self.assertEqual(len(files), 4)
                self.assertIn('"index":19', files[0].read_text())
                for path in files:
                    for line in path.read_text().splitlines():
                        json.loads(line)
            finally:
                await logs.close()

    async def test_write_failure_reports_once_without_dumping_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"])
            try:
                handler = logs.handlers["corp"]
                with (
                    mock.patch.object(
                        handler, "shouldRollover", side_effect=OSError("secret")
                    ),
                    self.assertLogs("hagency.model_proxy", level="ERROR") as captured,
                ):
                    await logs.emit("corp", {"body": "private-body"})
                    await logs.emit("corp", {"body": "private-body"})
                    await logs.flush()
                self.assertEqual(len(captured.output), 1)
                self.assertNotIn("private-body", str(captured.output))
                self.assertNotIn("secret", str(captured.output))
            finally:
                await logs.close()

    async def test_url_and_access_log_redaction(self):
        value = redact_url(
            "https://user:pass@host/v1?api_key=secret&key=secret&trace=keep"
        )
        self.assertNotIn("secret", value)
        self.assertNotIn("pass", value)
        self.assertIn("trace=keep", value)
        logger = logging.getLogger("test.proxy.access")
        request = mock.MagicMock(method="GET", path_qs="/v1/models?token=secret")
        request.get.return_value = None
        with self.assertLogs(logger, level="INFO") as captured:
            SafeAccessLogger(logger, "").log(request, mock.Mock(status=200), 0.1)
        self.assertNotIn("secret", str(captured.output))

    async def test_disk_flush_failure_cannot_escape_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = DebugLogs(Path(tmp), ["corp"])
            handler = logs.handlers["corp"]
            handler.stream.close()
            handler.stream = mock.Mock()
            handler.stream.write.side_effect = OSError("disk full")
            handler.stream.flush.side_effect = OSError("disk full")
            try:
                with (
                    mock.patch.object(handler, "shouldRollover", return_value=False),
                    self.assertLogs("hagency.model_proxy", level="ERROR"),
                ):
                    await logs.emit("corp", {"body": "private"})
                    await logs.flush()
                self.assertTrue(handler.failed)
            finally:
                await logs.close()


class DebugIntegrationTests(proxy_tests.ModelProxyIntegrationTests):
    """Run the existing HTTP/Hook/conversion contract suite with tracing enabled too."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.debug_directory = self.root / "debug"

    async def start_proxy(self, protocol, *, hook=False, forward=""):
        config = load_proxy_config(
            self.write_config(protocol, hook=hook, forward=forward)
        )
        self.proxy_client = TestClient(
            TestServer(
                create_model_proxy_app(config, debug_directory=self.debug_directory)
            )
        )
        await self.proxy_client.start_server()

    async def events(self):
        await self.proxy_client.server.app[LOGS_KEY].flush()
        return records(self.debug_directory)

    async def test_converted_stream_timeout_finishes_with_response_failed(self):
        await super().test_converted_stream_timeout_finishes_with_response_failed()
        events = [
            event
            for event in (await self.events())
            if event["stage"] == "client_response"
        ]
        self.assertEqual(events[-1]["event"], "end")
        self.assertFalse(events[-1]["complete"])
        self.assertEqual(events[-1]["bytes"], len(body(events, "client_response")))
        self.assertIn(b"response.failed", body(events, "client_response"))
        self.assertEqual(events[-1]["reason"], "upstream_timeout")
        self.assertEqual((await self.events())[-1]["reason"], "upstream_timeout")

    async def test_unmatched_routes_use_unrouted_file_and_preserve_http_errors(self):
        config = load_proxy_config(self.write_config(proxy_tests.PROTOCOL_CHAT))
        plain = TestClient(TestServer(create_model_proxy_app(config)))
        await plain.start_server()
        await self.start_proxy(proxy_tests.PROTOCOL_CHAT)
        try:
            for method, path in (
                ("GET", "/v1/nonexistent"),
                ("GET", "/corp/v1/nonexistent"),
                ("POST", "/v1/models"),
            ):
                expected = await plain.request(method, path)
                actual = await self.proxy_client.request(method, path)
                self.assertEqual(actual.status, expected.status)
                self.assertEqual(
                    actual.headers.get("Content-Type"),
                    expected.headers.get("Content-Type"),
                )
                self.assertEqual(
                    actual.headers.get("Allow"), expected.headers.get("Allow")
                )
                self.assertEqual(await actual.read(), await expected.read())
            await self.proxy_client.server.app[LOGS_KEY].flush()
            directory = self.debug_directory
            unrouted = [
                json.loads(line)
                for line in (directory / "unrouted.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len({event["request_id"] for event in unrouted}), 3)
            self.assertFalse(
                any(path.stat().st_size for path in directory.glob("provider-*.jsonl"))
            )
        finally:
            await plain.close()

    async def test_models_route_with_hook_fetch_models(self):
        await super().test_models_route_with_hook_fetch_models()
        events = await self.events()
        self.assertFalse(events[-1]["upstream_called"])
        self.assertTrue(body(events, "client_response"))

    async def test_cross_protocol_create_converts_request_and_response_but_rejects_extensions(
        self,
    ):
        await super().test_cross_protocol_create_converts_request_and_response_but_rejects_extensions()
        events = await self.events()
        first_id = events[0]["request_id"]
        first = [event for event in events if event["request_id"] == first_id]
        self.assertNotEqual(
            body(first, "client_request"), body(first, "upstream_request")
        )
        self.assertNotEqual(
            body(first, "upstream_response"), body(first, "client_response")
        )

    async def test_four_stages_capture_transport_headers_and_original_bytes(self):
        await self.start_proxy(proxy_tests.PROTOCOL_RESPONSES)
        raw = b'{ "model": "m", "input": "private prompt" }'
        response = await self.proxy_client.post(
            "/v1/responses?key=downstream-secret&trace=keep",
            data=raw,
            headers={
                "Authorization": "Bearer downstream-secret",
                "Cookie": "session=secret",
            },
        )
        result = await response.read()
        events = await self.events()
        self.assertEqual(len({event["request_id"] for event in events}), 1)
        for stage in ("client_request", "upstream_request"):
            self.assertEqual(body(events, stage), raw)
        for stage in ("upstream_response", "client_response"):
            self.assertEqual(body(events, stage), result)
        headers = next(
            event["headers"]
            for event in events
            if event["stage"] == "upstream_request" and event["event"] == "headers"
        )
        self.assertIn("User-Agent", dict(headers))
        self.assertEqual(dict(headers)["Authorization"], "<redacted>")
        self.assertNotIn("downstream-secret", json.dumps(events))
        self.assertNotIn("upstream-secret", json.dumps(events))
        self.assertEqual(
            self.requests[0]["headers"]["Authorization"], "Bearer upstream-secret"
        )
        self.assertEqual(self.requests[0]["body"], raw)
        self.assertEqual(
            {event["stage"] for event in events if event["event"] == "end"},
            {
                "client_request",
                "upstream_request",
                "upstream_response",
                "client_response",
                "exchange",
            },
        )

    async def test_stream_is_logged_and_delivered_before_upstream_finishes(self):
        release = asyncio.Event()
        prefix = b'data: {"delta":"first"}\n\n'
        suffix = b"data: [DONE]\n\n"

        async def streaming(request, _body):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(prefix)
            await release.wait()
            await response.write(suffix)
            return response

        self.behavior = streaming
        await self.start_proxy(proxy_tests.PROTOCOL_CHAT)
        response = await self.proxy_client.post(
            "/v1/chat/completions", json={"stream": True}
        )
        try:
            self.assertEqual(
                await asyncio.wait_for(response.content.readexactly(len(prefix)), 1),
                prefix,
            )
            self.assertEqual(body((await self.events()), "upstream_response"), prefix)
            self.assertEqual(body((await self.events()), "client_response"), prefix)
        finally:
            release.set()
        self.assertEqual(await response.read(), suffix)
        self.assertEqual(
            body((await self.events()), "client_response"), prefix + suffix
        )

    async def test_compressed_binary_and_head_responses(self):
        raw = gzip.compress(b"binary\x00\xff" * 40000)

        async def binary(_request, _body):
            return web.Response(body=raw, headers={"Content-Encoding": "gzip"})

        self.behavior = binary
        await self.start_proxy(proxy_tests.PROTOCOL_RESPONSES)
        response = await self.proxy_client.post(
            "/v1/responses",
            data=raw,
            headers={"Content-Encoding": "gzip"},
            auto_decompress=False,
        )
        self.assertEqual(await response.read(), raw)
        events = await self.events()
        for stage in (
            "client_request",
            "upstream_request",
            "upstream_response",
            "client_response",
        ):
            self.assertEqual(body(events, stage), raw)
        response = await self.proxy_client.head("/v1/responses/resource")
        self.assertEqual(await response.read(), b"")

    async def test_errors_without_upstream_and_models_are_recorded(self):
        await self.start_proxy(proxy_tests.PROTOCOL_CHAT)
        response = await self.proxy_client.get("/missing/v1/models")
        result = await response.read()
        events = await self.events()
        self.assertEqual(response.status, 404)
        self.assertEqual(body(events, "client_response"), result)
        self.assertFalse(events[-1]["upstream_called"])
        self.behavior = self.models_behavior
        response = await self.proxy_client.get("/v1/models")
        result = await response.read()
        events = [
            event for event in (await self.events()) if event["provider"] == "corp"
        ]
        self.assertEqual(body(events, "upstream_response"), result)
        self.assertEqual(body(events, "client_response"), result)

    async def test_concurrent_providers_have_separate_files_and_request_ids(self):
        config = load_proxy_config(self.write_config(proxy_tests.PROTOCOL_RESPONSES))
        config = replace(
            config,
            providers={
                **config.providers,
                "Corp": replace(config.providers["corp"], name="Corp"),
            },
        )
        self.proxy_client = TestClient(
            TestServer(
                create_model_proxy_app(config, debug_directory=self.debug_directory)
            )
        )
        await self.proxy_client.start_server()

        async def send(provider, index):
            response = await self.proxy_client.post(
                f"/{provider}/v1/responses", data=str(index).encode()
            )
            await response.read()

        await asyncio.gather(
            *(send(name, i) for name in ("corp", "Corp") for i in range(5))
        )
        await self.proxy_client.server.app[LOGS_KEY].flush()
        for path in self.debug_directory.glob("provider-*.jsonl"):
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len({event["provider"] for event in events}), 1)
            self.assertEqual(len({event["request_id"] for event in events}), 5)

    async def test_factory_uses_explicit_debug_directory(self):
        config = load_proxy_config(self.write_config(proxy_tests.PROTOCOL_CHAT))
        config = replace(config, debug=True)
        for directory in (None, self.debug_directory):
            with self.subTest(directory=directory):
                app = create_model_proxy_app(config, debug_directory=directory)
                async with TestClient(TestServer(app)) as client:
                    response = await client.get("/v1/models")
                    await response.read()
                    self.assertEqual(LOGS_KEY in app, directory is not None)
                    self.assertEqual(
                        self.debug_directory.exists(), directory is not None
                    )

    def test_config_debug_requires_boolean(self):
        path = self.write_config(proxy_tests.PROTOCOL_CHAT)
        original = path.read_text()
        for value in ('"true"', "1", "[]"):
            path.write_text(f"debug = {value}\n" + original)
            with self.assertRaisesRegex(
                ModelProxyConfigError, "debug: must be a boolean"
            ):
                load_proxy_config(path)

    async def test_internal_error_logs_frames_without_exception_text(self):
        config = load_proxy_config(self.write_config(proxy_tests.PROTOCOL_RESPONSES))

        async def unexpected_failure(*args):
            raise KeyError("private-message")

        for directory in (None, self.debug_directory):
            app = create_model_proxy_app(config, debug_directory=directory)
            async with TestClient(TestServer(app)) as client:
                for path, target in (
                    ("/v1/responses", "_proxy_request"),
                    ("/v1/models", "_models_request"),
                ):
                    with (
                        self.subTest(path=path, tracing=directory is not None),
                        mock.patch(
                            f"hagency_cli.model_proxy.server.{target}",
                            side_effect=unexpected_failure,
                        ),
                        self.assertLogs("hagency.model_proxy", level="ERROR") as captured,
                    ):
                        response = await client.get(path)
                        self.assertEqual(response.status, 500)
                        self.assertEqual(
                            (await response.json())["error"]["code"], "internal_error"
                        )
                        (message,) = captured.output
                        self.assertIn("error=KeyError", message)
                        self.assertRegex(
                            message, r"test_model_proxy_debug\.py:\d+:unexpected_failure"
                        )
                        self.assertNotIn("private-message", message)

    async def test_logging_failure_does_not_fail_http_exchange(self):
        await self.start_proxy(proxy_tests.PROTOCOL_RESPONSES)
        logs = self.proxy_client.server.app[LOGS_KEY]
        with (
            mock.patch.object(
                logs.handlers["corp"], "shouldRollover", side_effect=OSError()
            ),
            self.assertLogs("hagency.model_proxy", level="ERROR"),
        ):
            response = await self.proxy_client.post("/v1/responses", data=b"payload")
            self.assertEqual(response.status, 201)
            self.assertEqual(await response.read(), b'{ "z": 1, "unknown": [3,2,1] }')
            await logs.flush()

    async def test_auth_hook_metadata_is_redacted_but_upstream_receives_it(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text("""
from hagency_cli.model_proxy import AuthPatch, HeaderPatch, QueryPatch
class Hook:
    def __init__(self, init): pass
    async def authenticate(self, ctx, request):
        return AuthPatch(headers=HeaderPatch(set=(("X-Custom-Auth", "custom-secret"),)),
                         query=QueryPatch(set=(("ticket", "ticket-secret"),)))
""")

        async def reply(_request, _body):
            return web.Response(
                body=b"ok",
                headers={
                    "Set-Cookie": "cookie-secret",
                    "Location": "https://example.invalid/?token=location-secret",
                },
            )

        self.behavior = reply
        await self.start_proxy(proxy_tests.PROTOCOL_CHAT, hook=True)
        response = await self.proxy_client.post("/v1/chat/completions", json={})
        await response.read()
        metadata = json.dumps(await self.events())
        for secret in (
            "custom-secret",
            "ticket-secret",
            "cookie-secret",
            "location-secret",
        ):
            self.assertNotIn(secret, metadata)
        self.assertEqual(self.requests[0]["headers"]["X-Custom-Auth"], "custom-secret")
        self.assertIn("ticket=ticket-secret", self.requests[0]["query"])

    async def test_debug_initialization_failure_is_explicit(self):
        config = load_proxy_config(self.write_config(proxy_tests.PROTOCOL_CHAT))
        with (
            mock.patch(
                "hagency_cli.model_proxy.debug._TraceHandler._open",
                side_effect=OSError("secret"),
            ),
            self.assertRaisesRegex(RuntimeError, "could not initialize") as captured,
        ):
            create_model_proxy_app(config, debug_directory=self.debug_directory)
        self.assertNotIn("secret", str(captured.exception))

    async def test_invalid_auth_patch_still_returns_hook_error(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text("""
from hagency_cli.model_proxy import AuthPatch
class Hook:
    def __init__(self, init): pass
    async def authenticate(self, ctx, request):
        return AuthPatch(headers="invalid")
""")
        await self.start_proxy(proxy_tests.PROTOCOL_CHAT, hook=True)
        response = await self.proxy_client.post("/v1/chat/completions", json={})
        self.assertEqual(response.status, 502)
        self.assertEqual(
            (await response.json())["error"]["code"], "provider_hook_error"
        )

    async def test_oversized_request_is_logged_as_incomplete(self):
        with mock.patch("hagency_cli.model_proxy.server.MAX_REQUEST_BYTES", 8):
            await self.start_proxy(proxy_tests.PROTOCOL_RESPONSES)
            response = await self.proxy_client.post("/v1/responses", data=b"x" * 9)
            self.assertEqual(response.status, 413)
            await response.read()
        end = next(
            event
            for event in (await self.events())
            if event["stage"] == "client_request" and event["event"] == "end"
        )
        self.assertFalse(end["complete"])
        self.assertEqual(end["reason"], "request_too_large")

    async def test_cancellation_preserves_partial_trace(self):
        directory = self.root / "cancelled-traces"
        logs = DebugLogs(directory, ["corp"])
        request = make_mocked_request("POST", "/v1/responses")

        async def cancelled_handler(request):
            trace = request_trace(request)
            await trace.begin("upstream_response", status=200)
            await trace.body("upstream_response", b"partial")
            raise asyncio.CancelledError()

        try:
            with self.assertRaises(asyncio.CancelledError):
                await debug_middleware(logs, "corp")(request, cancelled_handler)
            await logs.flush()
            events = records(directory)
            self.assertEqual(body(events, "upstream_response"), b"partial")
            end = next(
                event
                for event in events
                if event["stage"] == "upstream_response" and event["event"] == "end"
            )
            self.assertFalse(end["complete"])
            self.assertEqual(events[-1]["reason"], "cancelled")
        finally:
            await logs.close()


class DebugLifecycleTests(unittest.TestCase):
    def test_real_worker_debug_cli_config_restart_and_access_redaction(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = b'{"data": [{"id": "local-model"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            config = root / "hagency-model-proxy.toml"
            original = (
                f'version = 1\ndefault_provider = "corp"\n[providers.corp]\n'
                f'adapter = "openai_compatible"\nbase_url = "http://127.0.0.1:{upstream.server_port}/v1"\n'
            )
            config.write_text(original)
            try:
                with mock.patch.dict(
                    os.environ, {"XDG_STATE_HOME": str(root / "state")}
                ):
                    try:
                        first, paths = daemon.start_model_proxy(
                            config, host="127.0.0.1", port=port, debug=True
                        )
                        self.assertTrue(first.debug)
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/v1/models?token=access-secret",
                            timeout=5,
                        ) as response:
                            self.assertIn(b"local-model", response.read())
                        config.write_text("debug = true\n" + original)
                        second, _ = daemon.restart_model_proxy(
                            config, host="127.0.0.1", port=port
                        )
                        self.assertTrue(second.debug)
                        config.write_text("debug = false\n" + original)
                        third, _ = daemon.restart_model_proxy(
                            config, host="127.0.0.1", port=port
                        )
                        self.assertFalse(third.debug)
                        self.assertNotEqual(first.pid, third.pid)
                        self.assertNotIn("access-secret", paths.log.read_text())
                        self.assertTrue(
                            body(records(paths.debug_directory), "client_response")
                        )
                    finally:
                        daemon.stop_model_proxy(config)
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join(timeout=5)

    def test_state_compatibility_and_debug_worker_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = daemon.ServiceState(
                pid=123,
                config=Path(tmp) / "config.toml",
                host="127.0.0.1",
                port=8765,
                started_at=1,
                process_identity="identity",
                startup_nonce="nonce",
                debug=True,
            )
            daemon.write_service_state(path, state)
            self.assertTrue(daemon._read_state(path).debug)
            raw = json.loads(path.read_text())
            raw.pop("debug")
            path.write_text(json.dumps(raw))
            self.assertFalse(daemon._read_state(path).debug)
            raw["debug"] = "true"
            path.write_text(json.dumps(raw))
            with self.assertRaises(daemon.ModelProxyServiceError):
                daemon._read_state(path)
            self.assertIn(
                "--debug",
                daemon._worker_command(
                    state.config, state.host, state.port, debug=True
                ),
            )
            self.assertNotIn(
                "--debug", daemon._worker_command(state.config, state.host, state.port)
            )


if __name__ == "__main__":
    unittest.main()
