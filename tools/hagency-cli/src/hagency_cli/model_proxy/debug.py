"""Provider-scoped, bounded-memory HTTP traces. Bodies are intentionally unfiltered."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from aiohttp.web_urldispatcher import SystemRoute

LOGGER = logging.getLogger("hagency.model_proxy")
# RequestKey was introduced after the minimum supported aiohttp version.
TRACE_KEY = getattr(web, "RequestKey", web.AppKey)("model_proxy_debug_trace", object)
LOGS_KEY = web.AppKey("model_proxy_debug_logs", object)
CHUNK_BYTES = 64 * 1024
REDACTED = "<redacted>"


def credential_name(name: str) -> bool:
    compact = "".join(char for char in name.lower() if char.isalnum())
    return compact in {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "key",
        "password",
        "passwd",
        "sig",
        "auth",
    } or any(
        marker in compact
        for marker in (
            "apikey",
            "token",
            "secret",
            "credential",
            "signature",
            "subscriptionkey",
        )
    )


def redact_url(url: str, extra: Iterable[str] = ()) -> str:
    try:
        parts = urlsplit(url)
        hidden = {name.lower() for name in extra}
        query = [
            (
                name,
                REDACTED if credential_name(name) or name.lower() in hidden else value,
            )
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))
    except ValueError:
        return "<invalid-url>"


class _TraceHandler(RotatingFileHandler):
    failed = False

    def _open(self):
        descriptor = os.open(
            self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
        )
        return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")

    def handleError(self, record: logging.LogRecord) -> None:
        # logging's default handler prints the failed record, including its body.
        if not self.failed:
            self.failed = True
            with contextlib.suppress(OSError, UnicodeError):
                self.close()
            LOGGER.error(
                "model proxy debug log write failed file=%s; tracing disabled for this file",
                Path(self.baseFilename).name,
            )


class DebugLogs:
    def __init__(
        self,
        directory: Path,
        providers: Iterable[str],
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 3,
        queue_size: int = 128,
    ) -> None:
        self.handlers: dict[str | None, _TraceHandler] = {}
        self.closed = False
        self._writer_failed = False
        self._enqueue_lock = asyncio.Lock()
        self._queue: asyncio.Queue[tuple[str | None, dict[str, Any]] | None] = (
            asyncio.Queue(queue_size)
        )
        self._writer: asyncio.Task[None] | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for provider in [None, *providers]:
                filename = "unrouted.jsonl"
                if provider is not None:
                    digest = hashlib.sha256(provider.encode()).hexdigest()[:16]
                    filename = f"provider-{provider[:64]}-{digest}.jsonl"
                self.handlers[provider] = _TraceHandler(
                    directory / filename,
                    maxBytes=max_bytes,
                    backupCount=backups,
                    encoding="utf-8",
                )
        except OSError as exc:
            self._close_handlers()
            detail = f": {os.strerror(exc.errno)}" if exc.errno is not None else ""
            raise RuntimeError(
                f"could not initialize model proxy debug logs{detail}"
            ) from None

    async def emit(self, provider: str | None, data: dict[str, Any]) -> None:
        if self.closed or self._writer_failed:
            return
        async with self._enqueue_lock:
            if self._writer_failed:
                return
            if self._writer is None:
                self._writer = asyncio.create_task(
                    self._write(), name="model-proxy-debug-writer"
                )
            # Await capacity when the disk is slower than the upstream stream.
            await self._queue.put((provider, data))

    async def _write(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < 64 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                if not self._writer_failed:
                    await asyncio.to_thread(self._write_batch, batch)
            except Exception as exc:
                # Keep draining so pending emits and shutdown can complete.
                self._writer_failed = True
                LOGGER.error(
                    "model proxy debug writer failed error=%s; tracing disabled",
                    type(exc).__name__,
                )
            finally:
                for _ in batch:
                    self._queue.task_done()
            if None in batch:
                return

    def _write_batch(
        self, batch: list[tuple[str | None, dict[str, Any]] | None]
    ) -> None:
        for entry in batch:
            if entry is not None:
                self._write_record(*entry)

    def _write_record(self, provider: str | None, data: dict[str, Any]) -> None:
        handler = self.handlers[provider if provider in self.handlers else None]
        if handler.failed:
            return
        message = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        handler.handle(
            logging.LogRecord(
                "hagency.model_proxy.debug", logging.DEBUG, "", 0, message, (), None
            )
        )

    async def flush(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._writer is not None:
            # Place the sentinel after emits already waiting to enqueue.
            async with self._enqueue_lock:
                await self._queue.put(None)
            await self._writer
        await asyncio.to_thread(self._close_handlers)

    def _close_handlers(self) -> None:
        for handler in self.handlers.values():
            with contextlib.suppress(OSError, UnicodeError):
                handler.close()


class RequestTrace:
    def __init__(self, logs: DebugLogs, provider: str | None) -> None:
        self.logs = logs
        self.provider = provider
        self.request_id = f"hgc_{uuid.uuid4().hex}"
        self.started = time.monotonic()
        self.stages: dict[str, dict[str, Any]] = {}
        self.secret_headers: set[str] = set()
        self.secret_query: set[str] = set()
        self.reason = "completed"

    async def emit(self, stage: str, event: str, **fields: Any) -> None:
        await self.logs.emit(
            self.provider,
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "request_id": self.request_id,
                "provider": self.provider,
                "stage": stage,
                "event": event,
                **fields,
            },
        )

    async def begin(self, stage: str, **fields: Any) -> None:
        if stage in self.stages:
            return
        self.stages[stage] = {"bytes": 0, "sequence": 0, "ended": False}
        if "url" in fields:
            fields["url"] = redact_url(fields["url"], self.secret_query)
        await self.emit(stage, "start", **fields)

    async def headers(self, stage: str, headers: Iterable[tuple[str, str]]) -> None:
        safe = []
        for name, value in headers:
            if credential_name(name) or name.lower() in self.secret_headers:
                value = REDACTED
            elif name.lower() in {"location", "content-location", "referer"}:
                value = redact_url(value, self.secret_query)
            safe.append((name, value))
        await self.emit(stage, "headers", headers=safe)

    async def body(self, stage: str, body: bytes) -> None:
        state = self.stages[stage]
        for offset in range(0, len(body), CHUNK_BYTES):
            chunk = body[offset : offset + CHUNK_BYTES]
            try:
                content, encoding = chunk.decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                content, encoding = base64.b64encode(chunk).decode("ascii"), "base64"
            await self.emit(
                stage,
                "body",
                sequence=state["sequence"],
                size=len(chunk),
                encoding=encoding,
                body=content,
            )
            state["bytes"] += len(chunk)
            state["sequence"] += 1

    async def end(
        self, stage: str, *, complete: bool = True, reason: str = "completed"
    ) -> None:
        state = self.stages.get(stage)
        if state is None or state["ended"]:
            return
        await self.emit(
            stage,
            "end",
            complete=complete,
            reason=reason,
            bytes=state["bytes"],
            elapsed_ms=(time.monotonic() - self.started) * 1000,
        )
        state["ended"] = True

    async def finish(self) -> None:
        for stage in self.stages:
            await self.end(
                stage,
                complete=False,
                reason=(
                    self.reason if self.reason != "completed" else "body_not_consumed"
                ),
            )
        await self.emit(
            "exchange",
            "end",
            reason=self.reason,
            upstream_called="upstream_request" in self.stages,
            elapsed_ms=(time.monotonic() - self.started) * 1000,
        )


def request_trace(request: web.Request) -> RequestTrace | None:
    return request.get(TRACE_KEY)


def trace_config() -> aiohttp.TraceConfig:
    config = aiohttp.TraceConfig()

    async def headers_sent(session, ctx, params):
        trace = ctx.trace_request_ctx
        if trace is not None:
            await trace.headers("upstream_request", params.headers.items())

    async def chunk_sent(session, ctx, params):
        trace = ctx.trace_request_ctx
        if trace is not None:
            await trace.body("upstream_request", params.chunk)

    async def request_end(session, ctx, params):
        if ctx.trace_request_ctx is not None:
            await ctx.trace_request_ctx.end("upstream_request")

    async def request_error(session, ctx, params):
        if ctx.trace_request_ctx is not None:
            await ctx.trace_request_ctx.end(
                "upstream_request", complete=False, reason="transport_error"
            )

    config.on_request_headers_sent.append(headers_sent)
    config.on_request_chunk_sent.append(chunk_sent)
    config.on_request_end.append(request_end)
    config.on_request_exception.append(request_error)
    return config


def debug_middleware(logs: DebugLogs, default_provider: str):
    @web.middleware
    async def trace_request(request: web.Request, handler):
        provider = None
        if not isinstance(request.match_info.route, SystemRoute):
            provider = request.match_info.get("provider") or default_provider
        trace = RequestTrace(logs, provider)
        request[TRACE_KEY] = trace
        try:
            await trace.begin(
                "client_request", method=request.method, url=request.path_qs
            )
            await trace.headers("client_request", request.headers.items())
            if not request.can_read_body:
                await trace.end("client_request")
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.Response(
                    status=exc.status,
                    reason=exc.reason,
                    headers=exc.headers,
                    body=exc.body,
                )
                trace.reason = "http_error"
            if isinstance(response, web.Response):
                # Sending here lets us distinguish completed writes from disconnects.
                await response.prepare(request)
                await response.write_eof()
                if request.method != "HEAD" and response.status not in {204, 304}:
                    body = response.body
                    if isinstance(body, bytes):
                        await trace.body("client_response", body)
                await trace.end("client_response")
            return response
        except asyncio.CancelledError:
            trace.reason = "cancelled"
            raise
        except Exception:
            trace.reason = "transport_or_handler_error"
            raise
        finally:
            await trace.finish()

    return trace_request


async def response_prepared(request: web.Request, response: web.StreamResponse) -> None:
    trace = request_trace(request)
    if trace is not None:
        await trace.begin("client_response", status=response.status)
        await trace.headers("client_response", response.headers.items())


class SafeAccessLogger(web.AbstractAccessLogger):
    def log(
        self, request: web.Request, response: web.StreamResponse, elapsed: float
    ) -> None:
        trace = request_trace(request)
        self.logger.info(
            "%s %s status=%s elapsed=%.3fs",
            request.method,
            redact_url(request.path_qs, trace.secret_query if trace else ()),
            response.status,
            elapsed,
        )
