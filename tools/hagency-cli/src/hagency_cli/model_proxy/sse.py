from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")


@dataclass(frozen=True)
class SseEvent:
    event: str | None = None
    data: bytes = b""
    id: str | None = None
    retry: int | None = None
    raw: bytes | None = None
    eof: bool = False

    def json(self) -> dict[str, Any]:
        value = json.loads(self.data)
        if not isinstance(value, dict):
            raise ValueError("SSE data must be a JSON object")
        return value

    def with_json(self, value: dict[str, Any], *, event: str | None = None) -> SseEvent:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return replace(
            self, event=self.event if event is None else event, data=data, raw=None
        )


class SseDecoder:
    def __init__(self, *, max_event_bytes: int = 8 * 1024 * 1024) -> None:
        self._buffer = bytearray()
        self._max_event_bytes = max_event_bytes

    def feed(self, chunk: bytes) -> list[SseEvent]:
        self._buffer.extend(chunk)
        events: list[SseEvent] = []
        while match := _SEPARATOR.search(self._buffer):
            end = match.end()
            if end > self._max_event_bytes:
                raise ValueError("SSE event exceeds size limit")
            raw = bytes(self._buffer[:end])
            del self._buffer[:end]
            events.append(parse_event(raw))
        if len(self._buffer) > self._max_event_bytes:
            raise ValueError("SSE event exceeds size limit")
        return events

    def finish(self) -> list[SseEvent]:
        if not self._buffer:
            return []
        if len(self._buffer) > self._max_event_bytes:
            raise ValueError("SSE event exceeds size limit")
        raw = bytes(self._buffer)
        self._buffer.clear()
        return [parse_event(raw)]


def parse_event(raw: bytes) -> SseEvent:
    data_lines: list[bytes] = []
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None
    stripped = _SEPARATOR.sub(b"", raw, count=1)
    for line in stripped.splitlines():
        if not line or line.startswith(b":"):
            continue
        field, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"data":
            data_lines.append(value)
        elif field == b"event":
            event = value.decode("utf-8", "replace")
        elif field == b"id" and b"\x00" not in value:
            event_id = value.decode("utf-8", "replace")
        elif field == b"retry":
            try:
                retry = int(value)
            except ValueError:
                pass
    return SseEvent(
        event=event, data=b"\n".join(data_lines), id=event_id, retry=retry, raw=raw
    )


def encode_event(event: SseEvent) -> bytes:
    if event.raw is not None:
        return event.raw
    if event.eof:
        return b""
    lines: list[bytes] = []
    if event.event is not None:
        lines.append(b"event: " + event.event.encode("utf-8"))
    if event.id is not None:
        lines.append(b"id: " + event.id.encode("utf-8"))
    if event.retry is not None:
        lines.append(b"retry: " + str(event.retry).encode("ascii"))
    for line in event.data.split(b"\n"):
        lines.append(b"data: " + line)
    return b"\n".join(lines) + b"\n\n"


def json_event(event_type: str | None, value: dict[str, Any]) -> SseEvent:
    return SseEvent(
        event=event_type,
        data=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
    )
