from __future__ import annotations

import asyncio
import functools
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from traceback import walk_tb
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy

from .config import ProviderConfig, ProxyConfig
from .conversion import (
    ChatToResponsesStream,
    ConversionError,
    ConversionWarnings,
    ResponsesToChatStream,
    UpstreamConversionError,
    convert_request,
    convert_response,
    normalized_error,
)
from .debug import (
    LOGS_KEY,
    DebugLogs,
    RequestTrace,
    debug_middleware,
    request_trace,
    response_prepared,
    trace_config,
)
from .hooks import (
    AuthPatch,
    FinalRequest,
    HeaderPatch,
    HeaderView,
    HookContext,
    HookReject,
    HookRuntime,
    ProviderRequest,
    ProviderResponse,
    QueryPatch,
    RequestPatch,
    ResponsePatch,
    load_hook,
)
from .providers import PROTOCOL_CHAT, PROTOCOL_RESPONSES
from .sse import SseDecoder, SseEvent, encode_event

LOGGER = logging.getLogger("hagency.model_proxy")
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_BUFFERED_RESPONSE_BYTES = 64 * 1024 * 1024
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
PROTECTED_REQUEST_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}
)
CREDENTIAL_HEADER_MARKERS = (
    "apikey",
    "token",
    "secret",
    "credential",
    "signature",
    "subscriptionkey",
)
BODY_INTEGRITY_HEADERS = frozenset(
    {"content-length", "content-encoding", "etag", "content-md5"}
)


class ProxyHttpError(Exception):
    def __init__(
        self, status: int, code: str, message: str, *, param: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param


class _DownstreamDisconnected(ConnectionError):
    pass


CONFIG_KEY = web.AppKey("model_proxy_config", ProxyConfig)
HOOKS_KEY = web.AppKey("model_proxy_hooks", dict[str, HookRuntime | None])
SESSION_KEY = web.AppKey("model_proxy_session", aiohttp.ClientSession)


def _request_id(request: web.Request) -> str:
    trace = request_trace(request)
    return trace.request_id if trace else f"hgc_{uuid.uuid4().hex}"


async def _read_client_body(request: web.Request) -> bytes:
    trace = request_trace(request)
    if trace is None:
        return await request.read()
    body = bytearray()
    async for chunk in request.content.iter_any():
        await trace.body("client_request", chunk)
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            await trace.end(
                "client_request", complete=False, reason="request_too_large"
            )
            raise web.HTTPRequestEntityTooLarge(MAX_REQUEST_BYTES, len(body))
    await trace.end("client_request")
    return bytes(body)


async def _upstream_chunks(
    upstream: aiohttp.ClientResponse,
    trace: RequestTrace | None,
) -> AsyncIterator[bytes]:
    complete = False
    try:
        async for chunk in upstream.content.iter_any():
            if trace is not None:
                await trace.body("upstream_response", chunk)
            yield chunk
        complete = True
    except asyncio.CancelledError:
        if trace is not None:
            trace.reason = "cancelled"
        raise
    except aiohttp.ClientConnectionError as exc:
        if trace is not None:
            trace.reason = _stream_failure_reason(exc)
        raise
    except (aiohttp.ClientError, ConnectionError) as exc:
        if trace is not None:
            trace.reason = "upstream_connection_error"
        raise aiohttp.ClientConnectionError("upstream response interrupted") from exc
    finally:
        if trace is not None:
            await trace.end(
                "upstream_response",
                complete=complete,
                reason="completed"
                if complete
                else (
                    trace.reason if trace.reason != "completed" else "body_not_consumed"
                ),
            )


def _trace_auth(request: web.Request, patch: AuthPatch) -> None:
    trace = request_trace(request)
    if trace is not None:
        trace.secret_headers.update(name.lower() for name, _ in patch.headers.set)
        trace.secret_query.update(name.lower() for name, _ in patch.query.set)


async def _trace_upstream(
    request: web.Request, upstream: aiohttp.ClientResponse
) -> None:
    trace = request_trace(request)
    if trace is not None:
        await trace.begin("upstream_response", status=upstream.status)
        await trace.headers("upstream_response", upstream.headers.items())


def _error_response(error: ProxyHttpError) -> web.Response:
    body = {
        "error": {
            "message": error.message,
            "type": "invalid_request_error" if error.status < 500 else "upstream_error",
            "param": error.param,
            "code": error.code,
        }
    }
    return web.Response(
        status=error.status,
        body=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        content_type="application/json",
    )


def _validate_header_pair(name: str, value: str) -> None:
    if (
        not isinstance(name, str)
        or not isinstance(value, str)
        or not name
        or any(char in name for char in "\r\n:")
        or any(char in value for char in "\r\n")
    ):
        raise ProxyHttpError(
            502, "provider_hook_error", "provider hook returned an invalid header"
        )


def _connection_tokens(headers: Iterable[tuple[str, str]]) -> set[str]:
    tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    return tokens


def _request_headers(
    request: web.Request, provider: ProviderConfig
) -> CIMultiDict[str]:
    raw = tuple(request.headers.items())
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(raw) | {"host", "content-length"}
    headers: CIMultiDict[str] = CIMultiDict()
    for name, value in raw:
        lowered = name.lower()
        if lowered in blocked:
            continue
        compact = "".join(char for char in lowered if char.isalnum())
        credential_like = lowered in PROTECTED_REQUEST_HEADERS or any(
            marker in compact for marker in CREDENTIAL_HEADER_MARKERS
        )
        if credential_like and lowered not in provider.forward_credential_headers:
            continue
        headers.add(name, value)
    return headers


def _response_headers(
    headers: CIMultiDictProxy[str], *, body_changed: bool
) -> CIMultiDict[str]:
    raw = tuple(headers.items())
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(raw)
    if body_changed:
        blocked |= BODY_INTEGRITY_HEADERS
    result: CIMultiDict[str] = CIMultiDict()
    for name, value in raw:
        if name.lower() not in blocked:
            result.add(name, value)
    return result


def _header_view(headers: CIMultiDict[str] | CIMultiDictProxy[str]) -> HeaderView:
    return HeaderView(tuple(headers.items()))


def _apply_header_patch(
    headers: CIMultiDict[str], patch: HeaderPatch, *, auth: bool = False
) -> None:
    if not isinstance(patch, HeaderPatch):
        raise ProxyHttpError(
            502, "provider_hook_error", "provider hook returned an invalid header patch"
        )
    forbidden = HOP_BY_HOP_HEADERS | {"host", "content-length"}
    for name in patch.remove:
        _validate_header_pair(name, "")
        if auth and name.lower() in forbidden:
            raise ProxyHttpError(
                502,
                "provider_hook_error",
                "authentication hook cannot change transport headers",
            )
        headers.popall(name, None)
    for name, value in patch.set:
        _validate_header_pair(name, value)
        if name.lower() in forbidden:
            raise ProxyHttpError(
                502,
                "provider_hook_error",
                "provider hook cannot change transport headers",
            )
        headers[name] = value


def _apply_query_patch(
    query: list[tuple[str, str]], patch: QueryPatch
) -> list[tuple[str, str]]:
    if not isinstance(patch, QueryPatch):
        raise ProxyHttpError(
            502, "provider_hook_error", "provider hook returned an invalid query patch"
        )
    removed = set(patch.remove)
    replaced = {name for name, _value in patch.set}
    result = [
        (name, value)
        for name, value in query
        if name not in removed and name not in replaced
    ]
    for name, value in patch.set:
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or any(char in name + value for char in "\r\n")
        ):
            raise ProxyHttpError(
                502,
                "provider_hook_error",
                "provider hook returned an invalid query value",
            )
        result.append((name, value))
    return result


def _url(base_url: str, relative_path: str, query: list[tuple[str, str]]) -> str:
    parsed = urlsplit(base_url)
    path = f"{parsed.path.rstrip('/')}/{relative_path.lstrip('/')}"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, urlencode(query, doseq=True), "")
    )


def _contains_dot_segment(path: str) -> bool:
    candidate = path
    for _attempt in range(len(path) + 1):
        if any(
            segment in {".", ".."}
            for segment in candidate.replace("\\", "/").split("/")
        ):
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _remove_request_body_integrity_headers(headers: CIMultiDict[str]) -> None:
    for name in ("Content-Encoding", "Content-MD5", "Digest"):
        headers.popall(name, None)


def _stream_requested(body: bytes) -> bool:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("stream") is True


def _include_usage(body: bytes) -> bool:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    options = value.get("stream_options") if isinstance(value, dict) else None
    return isinstance(options, dict) and options.get("include_usage") is True


def _log_warnings(
    warnings: ConversionWarnings,
    *,
    request_id: str,
    provider: str,
    source_protocol: str,
    target_protocol: str,
) -> None:
    for entry in warnings.entries:
        LOGGER.warning(
            json.dumps(
                {
                    "event": "model_proxy_conversion_warning",
                    "request_id": request_id,
                    "provider": provider,
                    "direction": f"{source_protocol}->{target_protocol}",
                    "json_pointer": entry.pointer,
                    "action": entry.action,
                    "reason": entry.reason,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


async def _call_hook(runtime: HookRuntime, method: Any, *args: object) -> Any:
    try:
        return await runtime.call(method, *args)
    except HookReject:
        raise
    except asyncio.TimeoutError as exc:
        raise ProxyHttpError(
            504, "provider_hook_timeout", "provider hook timed out"
        ) from exc
    except Exception as exc:
        LOGGER.error(
            "provider hook failed stage=%s error=%s",
            getattr(method, "__name__", "unknown"),
            type(exc).__name__,
        )
        raise ProxyHttpError(
            502, "provider_hook_error", "provider hook failed"
        ) from exc


async def _mapped_events(
    upstream: aiohttp.ClientResponse,
    *,
    mapper: Any,
    hook_runtime: HookRuntime | None,
    context: HookContext,
    converter: ChatToResponsesStream | ResponsesToChatStream | None,
    trace: RequestTrace | None = None,
) -> AsyncIterator[bytes]:
    decoder = SseDecoder()

    async def process(event: SseEvent) -> AsyncIterator[bytes]:
        events: Iterable[SseEvent]
        if mapper is not None:
            assert hook_runtime is not None
            try:
                mapped = await asyncio.wait_for(
                    mapper(context, event), timeout=hook_runtime.timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise ProxyHttpError(
                    504, "provider_hook_timeout", "provider SSE hook timed out"
                ) from exc
            except Exception as exc:
                LOGGER.error("provider SSE hook failed error=%s", type(exc).__name__)
                raise ProxyHttpError(
                    502, "provider_hook_error", "provider SSE hook failed"
                ) from exc
            events = (event,) if mapped is None else mapped
        else:
            events = (event,)
        for mapped_event in events:
            if converter is None:
                encoded = encode_event(mapped_event)
                if encoded:
                    yield encoded
            else:
                for converted in converter.feed(mapped_event):
                    yield encode_event(converted)

    async for chunk in _upstream_chunks(upstream, trace):
        try:
            events = decoder.feed(chunk)
        except ValueError as exc:
            raise UpstreamConversionError(str(exc)) from exc
        for event in events:
            async for output in process(event):
                yield output
    try:
        final_events = decoder.finish()
    except ValueError as exc:
        raise UpstreamConversionError(str(exc)) from exc
    for event in final_events:
        async for output in process(event):
            yield output
    if mapper is not None:
        async for output in process(SseEvent(eof=True)):
            yield output
    if converter is not None:
        for converted in converter.finish():
            yield encode_event(converted)


async def _read_buffered_response(
    upstream: aiohttp.ClientResponse,
    trace: RequestTrace | None = None,
) -> bytes:
    body = bytearray()
    async for chunk in _upstream_chunks(upstream, trace):
        body.extend(chunk)
        if len(body) > MAX_BUFFERED_RESPONSE_BYTES:
            raise ProxyHttpError(
                502, "upstream_response_too_large", "upstream response exceeds 64 MiB"
            )
    return bytes(body)


def _stream_failure_reason(error: BaseException) -> str:
    if isinstance(error, ProxyHttpError):
        return error.code
    if isinstance(error, aiohttp.ServerTimeoutError):
        return "upstream_timeout"
    if isinstance(error, UpstreamConversionError):
        return "invalid_upstream_response"
    if isinstance(error, aiohttp.ClientError):
        return "upstream_connection_error"
    if isinstance(error, ConnectionError):
        return "client_disconnected"
    return "stream_interrupted"


async def _stream_response(
    request: web.Request,
    upstream: aiohttp.ClientResponse,
    *,
    status: int,
    headers: CIMultiDict[str],
    iterator: AsyncIterator[bytes] | None,
    downstream_protocol: str,
    converted: bool,
    failure_event_factory: Callable[[], bytes | None] | None = None,
) -> web.StreamResponse:
    response = web.StreamResponse(status=status, headers=headers)
    trace = request_trace(request)

    async def write(chunk: bytes | None = None) -> None:
        try:
            if chunk is None:
                await response.write_eof()
            else:
                await response.write(chunk)
        except ConnectionError as exc:
            raise _DownstreamDisconnected() from exc
        if chunk is not None and trace is not None:
            await trace.body("client_response", chunk)

    async def finish(complete: bool, reason: str = "completed") -> None:
        if trace is not None:
            if not complete:
                trace.reason = reason
            await trace.end(
                "client_response",
                complete=complete,
                reason=reason,
            )

    if iterator is None:
        await response.prepare(request)
        try:
            async for chunk in _upstream_chunks(upstream, trace):
                await write(chunk)
            await write()
            await finish(True)
        except asyncio.CancelledError:
            upstream.close()
            raise
        except (
            ConnectionError,
            aiohttp.ServerTimeoutError,
            aiohttp.ClientConnectionError,
        ) as exc:
            await finish(False, _stream_failure_reason(exc))
            upstream.close()
            response.force_close()
            try:
                await response.write_eof()
            except ConnectionError:
                pass
        return response

    stream = iterator.__aiter__()
    try:
        first = await anext(stream)
    except StopAsyncIteration as exc:
        raise ProxyHttpError(
            502, "invalid_upstream_response", "upstream stream produced no output"
        ) from exc
    except UpstreamConversionError as exc:
        raise ProxyHttpError(502, "invalid_upstream_response", str(exc)) from exc
    except aiohttp.ServerTimeoutError as exc:
        raise ProxyHttpError(
            504, "upstream_timeout", "upstream request timed out"
        ) from exc
    except aiohttp.ClientConnectionError as exc:
        raise ProxyHttpError(
            502, "upstream_connection_error", "upstream stream disconnected"
        ) from exc
    await response.prepare(request)
    try:
        await write(first)
        async for chunk in stream:
            await write(chunk)
        await write()
        await finish(True)
    except asyncio.CancelledError:
        upstream.close()
        raise
    except (
        UpstreamConversionError,
        ProxyHttpError,
        aiohttp.ServerTimeoutError,
        aiohttp.ClientConnectionError,
    ) as exc:
        if (
            converted
            and downstream_protocol == PROTOCOL_RESPONSES
            and failure_event_factory is not None
        ):
            failed = failure_event_factory()
            try:
                if failed:
                    await write(failed)
                await response.write_eof()
            except ConnectionError:
                pass
        else:
            response.force_close()
            try:
                await response.write_eof()
            except ConnectionError:
                pass
        upstream.close()
        await finish(False, _stream_failure_reason(exc))
    except _DownstreamDisconnected:
        upstream.close()
        response.force_close()
        await finish(False, "client_disconnected")
    return response


async def _proxy_request(
    request: web.Request, downstream_protocol: str
) -> web.StreamResponse:
    config = request.app[CONFIG_KEY]
    provider_name = request.match_info.get("provider") or config.default_provider
    provider = config.providers.get(provider_name)
    if provider is None:
        raise ProxyHttpError(
            404, "unknown_provider", f"unknown provider: {provider_name}"
        )
    tail = request.match_info.get("tail", "")
    if _contains_dot_segment(tail):
        raise ProxyHttpError(
            400, "invalid_path", "resource path must not contain dot segments"
        )
    native = downstream_protocol == provider.protocol
    create_request = request.method == "POST" and not tail
    if not native and not create_request:
        raise ProxyHttpError(
            404,
            "unsupported_operation",
            "resource operation is only available for a native provider protocol",
        )

    request_id = _request_id(request)
    warnings = ConversionWarnings()
    try:
        body = await _read_client_body(request)
    except web.HTTPRequestEntityTooLarge as exc:
        raise ProxyHttpError(
            413, "request_too_large", "request body exceeds 64 MiB"
        ) from exc
    hook_runtime = request.app[HOOKS_KEY][provider_name]
    inspect_body = not native or hook_runtime is not None
    stream = _stream_requested(body) if inspect_body else False
    include_usage = (
        _include_usage(body)
        if downstream_protocol == PROTOCOL_CHAT and not native
        else False
    )
    upstream_body = body
    if not native:
        content_encoding = request.headers.get("Content-Encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            raise ProxyHttpError(
                400,
                "unsupported_content_encoding",
                "cross-protocol requests require an uncompressed JSON body",
                param="Content-Encoding",
            )
        try:
            upstream_body = convert_request(
                body, downstream_protocol, provider.protocol, warnings
            )
        except ConversionError as exc:
            raise ProxyHttpError(
                400, "unsupported_conversion", str(exc), param=exc.param
            ) from exc

    headers = _request_headers(request, provider)
    query = parse_qsl(request.query_string, keep_blank_values=True)
    relative_path = provider.adapter.operation_path(
        provider.protocol, tail if native else ""
    )
    initial_url = _url(provider.base_url, relative_path, query)
    context = HookContext(
        request_id=request_id,
        provider=provider_name,
        downstream_protocol=downstream_protocol,
        upstream_protocol=provider.protocol,
        stream=stream,
    )
    if hook_runtime is not None and hook_runtime.prepare_request is not None:
        hook_request = ProviderRequest(
            method=request.method,
            url=initial_url,
            headers=_header_view(headers),
            body=upstream_body,
            protocol=provider.protocol,
        )
        try:
            patch = await _call_hook(
                hook_runtime, hook_runtime.prepare_request, context, hook_request
            )
        except HookReject as exc:
            raise ProxyHttpError(exc.status, exc.code, exc.public_message) from exc
        if patch is not None:
            if not isinstance(patch, RequestPatch):
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "prepare_request returned an invalid patch",
                )
            if patch.json_body is not None:
                if not isinstance(patch.json_body, dict):
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "prepare_request returned an invalid JSON body",
                    )
                upstream_body = _json_bytes(patch.json_body)
                _remove_request_body_integrity_headers(headers)
                headers["Content-Type"] = "application/json"
            _apply_header_patch(headers, patch.headers)
            query = _apply_query_patch(query, patch.query)

    for name, value in provider.headers:
        headers[name] = value
    if not native:
        _remove_request_body_integrity_headers(headers)
        headers["Content-Type"] = "application/json"
        headers["Accept-Encoding"] = "identity"
    for name, value in provider.query:
        query = [(key, item) for key, item in query if key != name]
        query.append((name, value))
    final_url = _url(provider.base_url, relative_path, query)
    parsed_url = urlsplit(final_url)
    headers["Host"] = parsed_url.netloc
    headers["Content-Length"] = str(len(upstream_body))

    if hook_runtime is not None and hook_runtime.authenticate is not None:
        final_request = FinalRequest(
            method=request.method,
            url=final_url,
            headers=_header_view(headers),
            body=upstream_body,
            protocol=provider.protocol,
        )
        try:
            auth_patch = await _call_hook(
                hook_runtime, hook_runtime.authenticate, context, final_request
            )
        except HookReject as exc:
            raise ProxyHttpError(exc.status, exc.code, exc.public_message) from exc
        if auth_patch is not None:
            if not isinstance(auth_patch, AuthPatch):
                raise ProxyHttpError(
                    502, "provider_hook_error", "authenticate returned an invalid patch"
                )
            _apply_header_patch(headers, auth_patch.headers, auth=True)
            query = _apply_query_patch(query, auth_patch.query)
            _trace_auth(request, auth_patch)
            final_url = _url(provider.base_url, relative_path, query)

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=provider.connect_timeout_seconds,
        sock_connect=provider.connect_timeout_seconds,
        sock_read=provider.idle_timeout_seconds,
    )
    session = request.app[SESSION_KEY]
    trace = request_trace(request)
    if trace is not None:
        await trace.begin("upstream_request", method=request.method, url=final_url)
    try:
        upstream_context = session.request(
            request.method,
            final_url,
            headers=headers,
            data=upstream_body,
            allow_redirects=False,
            timeout=timeout,
            trace_request_ctx=trace,
        )
        async with upstream_context as upstream:
            await _trace_upstream(request, upstream)
            is_sse = (
                upstream.headers.get("Content-Type", "")
                .lower()
                .startswith("text/event-stream")
            )
            response_patch: ResponsePatch | None = None
            buffered_body: bytes | None = None
            if hook_runtime is not None and hook_runtime.process_response is not None:
                if not is_sse:
                    buffered_body = await _read_buffered_response(upstream, trace)
                provider_response = ProviderResponse(
                    status=upstream.status,
                    headers=_header_view(upstream.headers),
                    protocol=provider.protocol,
                    is_sse=is_sse,
                    body=buffered_body,
                )
                try:
                    response_patch = await _call_hook(
                        hook_runtime,
                        hook_runtime.process_response,
                        context,
                        provider_response,
                    )
                except HookReject as exc:
                    raise ProxyHttpError(
                        exc.status, exc.code, exc.public_message
                    ) from exc
                if response_patch is not None and not isinstance(
                    response_patch, ResponsePatch
                ):
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "process_response returned an invalid patch",
                    )

            status = (
                response_patch.status
                if response_patch is not None and response_patch.status is not None
                else upstream.status
            )
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not 100 <= status <= 599
            ):
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "process_response returned an invalid status",
                )
            body_changed = (
                bool(response_patch and response_patch.json_body is not None)
                or bool(response_patch and response_patch.sse_mapper is not None)
                or not native
            )
            response_headers = _response_headers(
                upstream.headers, body_changed=body_changed
            )
            if response_patch is not None:
                _apply_header_patch(response_headers, response_patch.headers)

            upstream_content_encoding = upstream.headers.get(
                "Content-Encoding", "identity"
            ).lower()
            if (
                not native
                and upstream_content_encoding not in {"", "identity"}
                and not (response_patch and response_patch.json_body is not None)
            ):
                raise ProxyHttpError(
                    502,
                    "unsupported_upstream_content_encoding",
                    "cross-protocol upstream response must be uncompressed",
                )

            if not native and not 200 <= status < 300:
                if response_patch is not None and response_patch.sse_mapper is not None:
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "error response hook cannot return sse_mapper",
                    )
                response_body = (
                    buffered_body
                    if buffered_body is not None
                    else await _read_buffered_response(upstream, trace)
                )
                if response_patch is not None and response_patch.json_body is not None:
                    if not isinstance(response_patch.json_body, dict):
                        raise ProxyHttpError(
                            502,
                            "provider_hook_error",
                            "process_response returned an invalid JSON body",
                        )
                    response_body = _json_bytes(response_patch.json_body)
                response_headers["Content-Type"] = "application/json"
                _log_warnings(
                    warnings,
                    request_id=request_id,
                    provider=provider_name,
                    source_protocol=downstream_protocol,
                    target_protocol=provider.protocol,
                )
                return web.Response(
                    status=status,
                    headers=response_headers,
                    body=normalized_error(response_body, status),
                )

            if is_sse:
                mapper = (
                    response_patch.sse_mapper if response_patch is not None else None
                )
                if response_patch is not None and response_patch.json_body is not None:
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "SSE response hook cannot return json_body",
                    )
                converter: ChatToResponsesStream | ResponsesToChatStream | None = None
                if not native:
                    response_headers["Content-Type"] = "text/event-stream"
                    if provider.protocol == PROTOCOL_CHAT:
                        converter = ChatToResponsesStream(warnings)
                    else:
                        converter = ResponsesToChatStream(
                            warnings, include_usage=include_usage
                        )
                iterator = None
                failure_event: Callable[[], bytes | None] | None = None
                if mapper is not None or converter is not None:
                    iterator = _mapped_events(
                        upstream,
                        mapper=mapper,
                        hook_runtime=hook_runtime,
                        context=context,
                        converter=converter,
                        trace=trace,
                    )
                if isinstance(converter, ChatToResponsesStream):

                    def emit_failure_event() -> bytes | None:
                        event = converter.failure("upstream stream failed")
                        return encode_event(event) if event is not None else None

                    failure_event = emit_failure_event

                result = await _stream_response(
                    request,
                    upstream,
                    status=status,
                    headers=response_headers,
                    iterator=iterator,
                    downstream_protocol=downstream_protocol,
                    converted=not native,
                    failure_event_factory=failure_event,
                )
                _log_warnings(
                    warnings,
                    request_id=request_id,
                    provider=provider_name,
                    source_protocol=downstream_protocol,
                    target_protocol=provider.protocol,
                )
                return result

            if (
                native
                and buffered_body is None
                and (response_patch is None or response_patch.json_body is None)
            ):
                return await _stream_response(
                    request,
                    upstream,
                    status=status,
                    headers=response_headers,
                    iterator=None,
                    downstream_protocol=downstream_protocol,
                    converted=False,
                )
            response_body = (
                buffered_body
                if buffered_body is not None
                else await _read_buffered_response(upstream, trace)
            )
            if response_patch is not None and response_patch.sse_mapper is not None:
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "non-SSE response hook cannot return sse_mapper",
                )
            if response_patch is not None and response_patch.json_body is not None:
                if not isinstance(response_patch.json_body, dict):
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "process_response returned an invalid JSON body",
                    )
                response_body = _json_bytes(response_patch.json_body)
            if not native:
                if 200 <= status < 300:
                    try:
                        response_body = convert_response(
                            response_body,
                            provider.protocol,
                            downstream_protocol,
                            warnings,
                        )
                    except UpstreamConversionError as exc:
                        raise ProxyHttpError(
                            502, "invalid_upstream_response", str(exc)
                        ) from exc
                else:
                    response_body = normalized_error(response_body, status)
                response_headers["Content-Type"] = "application/json"
            _log_warnings(
                warnings,
                request_id=request_id,
                provider=provider_name,
                source_protocol=downstream_protocol,
                target_protocol=provider.protocol,
            )
            return web.Response(
                status=status, headers=response_headers, body=response_body
            )
    except aiohttp.ServerTimeoutError as exc:
        raise ProxyHttpError(
            504, "upstream_timeout", "upstream request timed out"
        ) from exc
    except aiohttp.ClientConnectionError as exc:
        raise ProxyHttpError(
            502, "upstream_connection_error", "could not connect to upstream provider"
        ) from exc


async def _models_request(request: web.Request) -> web.StreamResponse:
    config = request.app[CONFIG_KEY]
    provider_name = request.match_info.get("provider") or config.default_provider
    provider = config.providers.get(provider_name)
    if provider is None:
        raise ProxyHttpError(
            404, "unknown_provider", f"unknown provider: {provider_name}"
        )

    request_id = _request_id(request)
    hook_runtime = request.app[HOOKS_KEY][provider_name]

    if hook_runtime is not None and hook_runtime.fetch_models is not None:
        context = HookContext(
            request_id=request_id,
            provider=provider_name,
            downstream_protocol="openai_models",
            upstream_protocol=provider.protocol,
            stream=False,
        )
        try:
            models = await _call_hook(hook_runtime, hook_runtime.fetch_models, context)
        except HookReject as exc:
            raise ProxyHttpError(exc.status, exc.code, exc.public_message) from exc
        if models is None:
            raise ProxyHttpError(
                404,
                "unsupported_operation",
                "provider hook returned no model list",
            )
        if not isinstance(models, list):
            raise ProxyHttpError(
                502,
                "provider_hook_error",
                "fetch_models returned an invalid result",
            )
        if any(not isinstance(model, str) or not model for model in models):
            raise ProxyHttpError(
                502,
                "provider_hook_error",
                "fetch_models returned an invalid model id",
            )
        body = {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": provider_name,
                }
                for model in models
            ],
        }
        return web.Response(
            status=200,
            body=_json_bytes(body),
            content_type="application/json",
        )

    models_path = provider.adapter.models_path
    if models_path is None:
        raise ProxyHttpError(
            404,
            "unsupported_operation",
            "this provider does not expose a models endpoint",
        )

    headers = _request_headers(request, provider)
    query = parse_qsl(request.query_string, keep_blank_values=True)
    context = HookContext(
        request_id=request_id,
        provider=provider_name,
        downstream_protocol="openai_models",
        upstream_protocol=provider.protocol,
        stream=False,
    )
    initial_url = _url(provider.base_url, models_path, query)
    if hook_runtime is not None and hook_runtime.prepare_request is not None:
        hook_request = ProviderRequest(
            method="GET",
            url=initial_url,
            headers=_header_view(headers),
            body=b"",
            protocol=provider.protocol,
        )
        try:
            patch = await _call_hook(
                hook_runtime, hook_runtime.prepare_request, context, hook_request
            )
        except HookReject as exc:
            raise ProxyHttpError(exc.status, exc.code, exc.public_message) from exc
        if patch is not None:
            if not isinstance(patch, RequestPatch):
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "prepare_request returned an invalid patch",
                )
            if patch.json_body is not None:
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "prepare_request cannot add a body to a models request",
                )
            _apply_header_patch(headers, patch.headers)
            query = _apply_query_patch(query, patch.query)

    for name, value in provider.headers:
        headers[name] = value
    for name, value in provider.query:
        query = [(key, item) for key, item in query if key != name]
        query.append((name, value))
    final_url = _url(provider.base_url, models_path, query)
    parsed_url = urlsplit(final_url)
    headers["Host"] = parsed_url.netloc

    if hook_runtime is not None and hook_runtime.authenticate is not None:
        final_request = FinalRequest(
            method="GET",
            url=final_url,
            headers=_header_view(headers),
            body=b"",
            protocol=provider.protocol,
        )
        try:
            auth_patch = await _call_hook(
                hook_runtime, hook_runtime.authenticate, context, final_request
            )
        except HookReject as exc:
            raise ProxyHttpError(exc.status, exc.code, exc.public_message) from exc
        if auth_patch is not None:
            if not isinstance(auth_patch, AuthPatch):
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "authenticate returned an invalid patch",
                )
            _apply_header_patch(headers, auth_patch.headers, auth=True)
            query = _apply_query_patch(query, auth_patch.query)
            _trace_auth(request, auth_patch)
            final_url = _url(provider.base_url, models_path, query)

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=provider.connect_timeout_seconds,
        sock_connect=provider.connect_timeout_seconds,
        sock_read=provider.idle_timeout_seconds,
    )
    session = request.app[SESSION_KEY]
    trace = request_trace(request)
    if trace is not None:
        await trace.begin("upstream_request", method=request.method, url=final_url)
    try:
        async with session.get(
            final_url,
            headers=headers,
            allow_redirects=False,
            timeout=timeout,
            trace_request_ctx=trace,
        ) as upstream:
            await _trace_upstream(request, upstream)
            body = await _read_buffered_response(upstream, trace)
            response_patch: ResponsePatch | None = None
            if hook_runtime is not None and hook_runtime.process_response is not None:
                provider_response = ProviderResponse(
                    status=upstream.status,
                    headers=_header_view(upstream.headers),
                    protocol=provider.protocol,
                    is_sse=False,
                    body=body,
                )
                try:
                    response_patch = await _call_hook(
                        hook_runtime,
                        hook_runtime.process_response,
                        context,
                        provider_response,
                    )
                except HookReject as exc:
                    raise ProxyHttpError(
                        exc.status, exc.code, exc.public_message
                    ) from exc
                if response_patch is not None and not isinstance(
                    response_patch, ResponsePatch
                ):
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "process_response returned an invalid patch",
                    )

            status = (
                response_patch.status
                if response_patch is not None and response_patch.status is not None
                else upstream.status
            )
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not 100 <= status <= 599
            ):
                raise ProxyHttpError(
                    502,
                    "provider_hook_error",
                    "process_response returned an invalid status",
                )
            body_changed = bool(
                response_patch is not None and response_patch.json_body is not None
            )
            response_headers = _response_headers(
                upstream.headers, body_changed=body_changed
            )
            if response_patch is not None:
                if response_patch.sse_mapper is not None:
                    raise ProxyHttpError(
                        502,
                        "provider_hook_error",
                        "models response hook cannot return sse_mapper",
                    )
                _apply_header_patch(response_headers, response_patch.headers)
                if response_patch.json_body is not None:
                    if not isinstance(response_patch.json_body, dict):
                        raise ProxyHttpError(
                            502,
                            "provider_hook_error",
                            "process_response returned an invalid JSON body",
                        )
                    body = _json_bytes(response_patch.json_body)
                    response_headers["Content-Type"] = "application/json"
            return web.Response(
                status=status,
                headers=response_headers,
                body=body,
            )
    except aiohttp.ServerTimeoutError as exc:
        raise ProxyHttpError(
            504, "upstream_timeout", "upstream request timed out"
        ) from exc
    except aiohttp.ClientConnectionError as exc:
        raise ProxyHttpError(
            502, "upstream_connection_error", "could not connect to upstream provider"
        ) from exc


def _internal_error_response(
    request: web.Request, operation: str, error: Exception
) -> web.Response:
    if (trace := request_trace(request)) is not None:
        trace.reason = "internal_error"
    frames = " -> ".join(
        f"{Path(frame.f_code.co_filename).name}:{lineno}:{frame.f_code.co_name}"
        for frame, lineno in walk_tb(error.__traceback__)
    )
    LOGGER.error(
        "model proxy %s failed error=%s frames=%s",
        operation,
        type(error).__name__,
        frames,
    )
    return _error_response(
        ProxyHttpError(500, "internal_error", "model proxy request failed")
    )


async def _models_route(request: web.Request) -> web.StreamResponse:
    try:
        return await _models_request(request)
    except ProxyHttpError as exc:
        if (trace := request_trace(request)) is not None:
            trace.reason = exc.code
        return _error_response(exc)
    except web.HTTPException as exc:
        if (trace := request_trace(request)) is not None:
            trace.reason = "http_error"
        return _error_response(ProxyHttpError(exc.status, "http_error", exc.reason))
    except Exception as exc:
        return _internal_error_response(request, "models request", exc)


async def _route(request: web.Request, *, protocol: str) -> web.StreamResponse:
    try:
        return await _proxy_request(request, protocol)
    except ProxyHttpError as exc:
        if (trace := request_trace(request)) is not None:
            trace.reason = exc.code
        return _error_response(exc)
    except web.HTTPException as exc:
        if (trace := request_trace(request)) is not None:
            trace.reason = "http_error"
        return _error_response(ProxyHttpError(exc.status, "http_error", exc.reason))
    except Exception as exc:
        return _internal_error_response(request, "request", exc)


async def _start_session(app: web.Application) -> None:
    app[SESSION_KEY] = aiohttp.ClientSession(
        auto_decompress=False,
        trust_env=False,
        trace_configs=[trace_config()] if LOGS_KEY in app else [],
    )


async def _close_session(app: web.Application) -> None:
    await app[SESSION_KEY].close()


def create_model_proxy_app(
    config: ProxyConfig, *, debug_directory: Path | None = None
) -> web.Application:
    hooks = {
        name: load_hook(provider, config.path, config.env, LOGGER)
        for name, provider in config.providers.items()
    }
    logs = (
        DebugLogs(debug_directory, config.providers)
        if debug_directory is not None
        else None
    )
    app = web.Application(
        middlewares=[debug_middleware(logs, config.default_provider)] if logs else [],
        client_max_size=MAX_REQUEST_BYTES,
        handler_args={"auto_decompress": False},
    )
    if logs is not None:
        app[LOGS_KEY] = logs
        app.on_response_prepare.append(response_prepared)

        async def close_logs(app: web.Application) -> None:
            await logs.close()

        app.on_cleanup.append(close_logs)
    app[CONFIG_KEY] = config
    app[HOOKS_KEY] = hooks
    app.on_startup.append(_start_session)
    app.on_cleanup.append(_close_session)
    routes = (
        ("/v1/responses", PROTOCOL_RESPONSES),
        ("/v1/responses/{tail:.*}", PROTOCOL_RESPONSES),
        ("/v1/chat/completions", PROTOCOL_CHAT),
        ("/v1/chat/completions/{tail:.*}", PROTOCOL_CHAT),
        ("/{provider}/v1/responses", PROTOCOL_RESPONSES),
        ("/{provider}/v1/responses/{tail:.*}", PROTOCOL_RESPONSES),
        ("/{provider}/v1/chat/completions", PROTOCOL_CHAT),
        ("/{provider}/v1/chat/completions/{tail:.*}", PROTOCOL_CHAT),
    )
    for path, protocol in routes:
        app.router.add_route("*", path, functools.partial(_route, protocol=protocol))
    for models_path in ("/v1/models", "/{provider}/v1/models"):
        app.router.add_route("GET", models_path, _models_route)
    return app
