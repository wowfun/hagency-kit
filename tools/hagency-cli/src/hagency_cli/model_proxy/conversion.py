from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .providers import PROTOCOL_CHAT, PROTOCOL_RESPONSES
from .sse import SseEvent, json_event


class ConversionError(ValueError):
    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


class UpstreamConversionError(ValueError):
    pass


@dataclass(frozen=True)
class ConversionWarning:
    pointer: str
    action: str
    reason: str


@dataclass
class ConversionWarnings:
    entries: list[ConversionWarning] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)

    def add(self, pointer: str, action: str, reason: str) -> None:
        key = (pointer, reason)
        if key in self._seen:
            return
        self._seen.add(key)
        self.entries.append(
            ConversionWarning(pointer=pointer, action=action, reason=reason)
        )


def _object(body: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ConversionError(f"{label} must be a JSON object")
    return value


def _dump(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _copy_fields(
    source: dict[str, Any], target: dict[str, Any], fields: set[str], used: set[str]
) -> None:
    for name in fields:
        if name in source:
            target[name] = source[name]
            used.add(name)


def _warn_unknown(
    source: dict[str, Any], used: set[str], warnings: ConversionWarnings
) -> None:
    for name in source:
        if name not in used:
            warnings.add(f"/{name}", "dropped", "field has no cross-protocol mapping")


def _chat_part_to_responses(
    part: Any, warnings: ConversionWarnings, pointer: str
) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        warnings.add(pointer, "dropped", "content part is not an object")
        return None
    kind = part.get("type")
    if kind in ("text", "input_text") and isinstance(part.get("text"), str):
        return {"type": "input_text", "text": part["text"]}
    if kind == "image_url" and isinstance(part.get("image_url"), dict):
        image = part["image_url"]
        url = image.get("url")
        if isinstance(url, str):
            converted: dict[str, Any] = {"type": "input_image", "image_url": url}
            if image.get("detail") is not None:
                converted["detail"] = image["detail"]
            return converted
    if kind == "file" and isinstance(part.get("file"), dict):
        file_part = part["file"]
        converted = {"type": "input_file"}
        for name in ("file_id", "file_data", "filename"):
            if name in file_part:
                converted[name] = file_part[name]
        if len(converted) > 1:
            return converted
    if kind in ("input_audio", "audio"):
        warnings.add(
            pointer, "dropped", "audio content has no equivalent Responses input part"
        )
        return None
    warnings.add(pointer, "dropped", "unsupported content part")
    return None


def _responses_part_to_chat(
    part: Any, warnings: ConversionWarnings, pointer: str
) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        warnings.add(pointer, "dropped", "content part is not an object")
        return None
    kind = part.get("type")
    if kind in ("input_text", "output_text", "text") and isinstance(
        part.get("text"), str
    ):
        return {"type": "text", "text": part["text"]}
    if kind == "input_image":
        url = part.get("image_url")
        if isinstance(url, str):
            image: dict[str, Any] = {"url": url}
            if part.get("detail") is not None:
                image["detail"] = part["detail"]
            return {"type": "image_url", "image_url": image}
    if kind == "input_file":
        file_part: dict[str, Any] = {}
        for name in ("file_id", "file_data", "filename"):
            if name in part:
                file_part[name] = part[name]
        if file_part:
            return {"type": "file", "file": file_part}
    warnings.add(pointer, "dropped", "unsupported content part")
    return None


def _chat_content_to_responses(
    content: Any, warnings: ConversionWarnings, pointer: str
) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    converted = [
        item
        for index, part in enumerate(content)
        if (item := _chat_part_to_responses(part, warnings, f"{pointer}/{index}"))
        is not None
    ]
    return converted


def _responses_content_to_chat(
    content: Any, warnings: ConversionWarnings, pointer: str
) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    converted = [
        item
        for index, part in enumerate(content)
        if (item := _responses_part_to_chat(part, warnings, f"{pointer}/{index}"))
        is not None
    ]
    if converted and all(item.get("type") == "text" for item in converted):
        return "".join(item["text"] for item in converted)
    return converted


def _chat_tools_to_responses(
    tools: Any, warnings: ConversionWarnings
) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        raise ConversionError("tools must be an array", param="tools")
    converted: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        pointer = f"/tools/{index}"
        if not isinstance(tool, dict):
            warnings.add(pointer, "dropped", "tool is not an object")
            continue
        kind = tool.get("type")
        nested = tool.get(kind) if kind in ("function", "custom") else None
        if kind in ("function", "custom") and isinstance(nested, dict):
            converted.append({"type": kind, **nested})
        else:
            warnings.add(pointer, "dropped", "tool type has no cross-protocol mapping")
    return converted


def _responses_tools_to_chat(
    tools: Any, warnings: ConversionWarnings
) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        raise ConversionError("tools must be an array", param="tools")
    converted: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        pointer = f"/tools/{index}"
        if not isinstance(tool, dict):
            warnings.add(pointer, "dropped", "tool is not an object")
            continue
        kind = tool.get("type")
        if kind in ("function", "custom"):
            nested = {key: value for key, value in tool.items() if key != "type"}
            converted.append({"type": kind, kind: nested})
        elif kind == "web_search":
            warnings.add(
                pointer,
                "approximated",
                "Responses web_search becomes Chat web_search_options",
            )
        else:
            warnings.add(pointer, "dropped", "tool type has no cross-protocol mapping")
    return converted


def _tool_choice_chat_to_responses(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    kind = choice.get("type")
    nested = choice.get(kind)
    if kind in ("function", "custom") and isinstance(nested, dict):
        return {"type": kind, **nested}
    return choice


def _tool_choice_responses_to_chat(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    kind = choice.get("type")
    if kind in ("function", "custom"):
        return {
            "type": kind,
            kind: {key: value for key, value in choice.items() if key != "type"},
        }
    return choice


def _response_format_chat_to_responses(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("type") != "json_schema":
        return value
    schema = value.get("json_schema")
    if not isinstance(schema, dict):
        raise ConversionError(
            "json_schema response_format must contain a json_schema object",
            param="response_format",
        )
    return {
        "type": "json_schema",
        **{name: item for name, item in schema.items() if name != "type"},
    }


def _response_format_responses_to_chat(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("type") != "json_schema":
        return value
    return {
        "type": "json_schema",
        "json_schema": {name: item for name, item in value.items() if name != "type"},
    }


def chat_request_to_responses(
    source: dict[str, Any], warnings: ConversionWarnings
) -> dict[str, Any]:
    used: set[str] = set()
    target: dict[str, Any] = {}
    _copy_fields(
        source,
        target,
        {
            "model",
            "stream",
            "temperature",
            "top_p",
            "metadata",
            "service_tier",
            "parallel_tool_calls",
            "prompt_cache_key",
            "safety_identifier",
            "store",
        },
        used,
    )
    stream_options = source.get("stream_options")
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise ConversionError(
                "stream_options must be an object", param="stream_options"
            )
        if "include_obfuscation" in stream_options:
            target["stream_options"] = {
                "include_obfuscation": stream_options["include_obfuscation"]
            }
        used.add("stream_options")
    if "max_completion_tokens" in source and "max_tokens" in source:
        raise ConversionError(
            "max_completion_tokens and max_tokens cannot both be set",
            param="max_completion_tokens",
        )
    if "max_completion_tokens" in source:
        target["max_output_tokens"] = source["max_completion_tokens"]
        used.add("max_completion_tokens")
    elif "max_tokens" in source:
        target["max_output_tokens"] = source["max_tokens"]
        used.add("max_tokens")
    if "response_format" in source:
        target["text"] = {
            "format": _response_format_chat_to_responses(source["response_format"])
        }
        used.add("response_format")
    if "verbosity" in source:
        target.setdefault("text", {})["verbosity"] = source["verbosity"]
        used.add("verbosity")
    if "reasoning_effort" in source:
        target["reasoning"] = {"effort": source["reasoning_effort"]}
        used.add("reasoning_effort")
    if "tools" in source:
        target["tools"] = _chat_tools_to_responses(source["tools"], warnings)
        used.add("tools")
    if "tool_choice" in source:
        target["tool_choice"] = _tool_choice_chat_to_responses(source["tool_choice"])
        used.add("tool_choice")
    if source.get("web_search_options") is not None:
        target.setdefault("tools", []).append(
            {"type": "web_search", **source["web_search_options"]}
        )
        used.add("web_search_options")
        warnings.add(
            "/web_search_options",
            "approximated",
            "Chat web search becomes a Responses web_search tool",
        )
    if source.get("tool_choice") == "required" and not target.get("tools"):
        raise ConversionError(
            "required tool choice has no supported tools after conversion",
            param="tool_choice",
        )

    if source.get("n", 1) not in (None, 1):
        warnings.add("/n", "approximated", "Responses returns one candidate")
    if "n" in source:
        used.add("n")
    modalities = source.get("modalities")
    if modalities is not None and modalities != ["text"]:
        raise ConversionError(
            "only text output modality can be converted to Responses",
            param="modalities",
        )
    if "modalities" in source:
        used.add("modalities")
    if source.get("audio") is not None:
        raise ConversionError(
            "audio output cannot be converted to Responses", param="audio"
        )

    messages = source.get("messages")
    if not isinstance(messages, list):
        raise ConversionError("messages must be an array", param="messages")
    used.add("messages")
    items: list[dict[str, Any]] = []
    call_kinds: dict[str, str] = {}
    for index, message in enumerate(messages):
        pointer = f"/messages/{index}"
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ConversionError("each message must contain a role", param=pointer)
        role = message["role"]
        content = _chat_content_to_responses(
            message.get("content"), warnings, f"{pointer}/content"
        )
        if role in {"system", "developer", "user"}:
            if content not in (None, "", []):
                items.append({"role": role, "content": content})
        elif role == "assistant":
            if content not in (None, "", []):
                items.append({"role": "assistant", "content": content})
            calls = message.get("tool_calls", [])
            if calls is not None and not isinstance(calls, list):
                raise ConversionError(
                    "tool_calls must be an array", param=f"{pointer}/tool_calls"
                )
            for call_index, call in enumerate(calls or []):
                if not isinstance(call, dict):
                    raise ConversionError(
                        "tool call must be an object",
                        param=f"{pointer}/tool_calls/{call_index}",
                    )
                kind = call.get("type", "function")
                nested = call.get(kind)
                call_id = call.get("id")
                if (
                    kind not in ("function", "custom")
                    or not isinstance(nested, dict)
                    or not isinstance(call_id, str)
                ):
                    warnings.add(
                        f"{pointer}/tool_calls/{call_index}",
                        "dropped",
                        "unsupported tool call",
                    )
                    continue
                item = {
                    "type": "custom_tool_call" if kind == "custom" else "function_call",
                    "call_id": call_id,
                    **nested,
                }
                items.append(item)
                call_kinds[call_id] = kind
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str):
                raise ConversionError(
                    "tool message requires tool_call_id",
                    param=f"{pointer}/tool_call_id",
                )
            kind = call_kinds.get(call_id, "function")
            output = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            )
            items.append(
                {
                    "type": "custom_tool_call_output"
                    if kind == "custom"
                    else "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
        else:
            warnings.add(
                f"{pointer}/role",
                "dropped",
                "message role has no cross-protocol mapping",
            )
    if not items:
        raise ConversionError(
            "conversion produced no usable input messages", param="messages"
        )
    target["input"] = items
    _warn_unknown(source, used, warnings)
    return target


def responses_request_to_chat(
    source: dict[str, Any], warnings: ConversionWarnings
) -> dict[str, Any]:
    for name in ("background", "conversation", "previous_response_id", "prompt"):
        if source.get(name) is not None and source.get(name) is not False:
            raise ConversionError(
                f"{name} requires Responses server state and cannot be converted",
                param=name,
            )
    used = {
        name
        for name in ("background", "conversation", "previous_response_id", "prompt")
        if name in source
    }
    target: dict[str, Any] = {}
    _copy_fields(
        source,
        target,
        {
            "model",
            "stream",
            "temperature",
            "top_p",
            "metadata",
            "service_tier",
            "parallel_tool_calls",
            "prompt_cache_key",
            "safety_identifier",
        },
        used,
    )
    stream_options = source.get("stream_options")
    target_stream_options: dict[str, Any] = {}
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise ConversionError(
                "stream_options must be an object", param="stream_options"
            )
        if "include_obfuscation" in stream_options:
            target_stream_options["include_obfuscation"] = stream_options[
                "include_obfuscation"
            ]
        used.add("stream_options")
    if source.get("stream") is True:
        target_stream_options["include_usage"] = True
    if target_stream_options:
        target["stream_options"] = target_stream_options
    if "max_output_tokens" in source:
        target["max_completion_tokens"] = source["max_output_tokens"]
        used.add("max_output_tokens")
    text = source.get("text")
    if text is not None:
        if not isinstance(text, dict):
            raise ConversionError("text must be an object", param="text")
        if "format" in text:
            target["response_format"] = _response_format_responses_to_chat(
                text["format"]
            )
        if "verbosity" in text:
            target["verbosity"] = text["verbosity"]
        for name in text:
            if name not in {"format", "verbosity"}:
                warnings.add(
                    f"/text/{name}", "dropped", "text option has no Chat mapping"
                )
        used.add("text")
    reasoning = source.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, dict):
            raise ConversionError("reasoning must be an object", param="reasoning")
        if "effort" in reasoning:
            target["reasoning_effort"] = reasoning["effort"]
        for name in reasoning:
            if name != "effort":
                warnings.add(
                    f"/reasoning/{name}",
                    "dropped",
                    "reasoning option has no Chat mapping",
                )
        used.add("reasoning")
    if "tools" in source:
        target["tools"] = _responses_tools_to_chat(source["tools"], warnings)
        for tool in source["tools"]:
            if isinstance(tool, dict) and tool.get("type") == "web_search":
                target["web_search_options"] = {
                    key: value for key, value in tool.items() if key != "type"
                }
                break
        if not target["tools"]:
            target.pop("tools")
        used.add("tools")
    if "tool_choice" in source:
        if isinstance(source["tool_choice"], dict) and source["tool_choice"].get(
            "type"
        ) not in ("function", "custom"):
            raise ConversionError(
                "forced tool choice has no Chat mapping", param="tool_choice"
            )
        target["tool_choice"] = _tool_choice_responses_to_chat(source["tool_choice"])
        used.add("tool_choice")
    if source.get("store") is True:
        warnings.add(
            "/store",
            "dropped",
            "Responses and Chat storage resources are not interchangeable",
        )
    if "store" in source:
        target["store"] = False
        used.add("store")

    input_value = source.get("input")
    used.add("input")
    if isinstance(input_value, str):
        items: list[Any] = [{"role": "user", "content": input_value}]
    elif isinstance(input_value, list):
        items = input_value
    else:
        raise ConversionError("input must be a string or array", param="input")
    messages: list[dict[str, Any]] = []
    if isinstance(source.get("instructions"), str) and source["instructions"]:
        messages.append({"role": "developer", "content": source["instructions"]})
    if "instructions" in source:
        used.add("instructions")
    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        if pending_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(pending_calls),
                }
            )
            pending_calls.clear()

    for index, item in enumerate(items):
        pointer = f"/input/{index}"
        if not isinstance(item, dict):
            warnings.add(pointer, "dropped", "input item is not an object")
            continue
        kind = item.get("type")
        if kind in (None, "message") and item.get("role") in (
            "system",
            "developer",
            "user",
            "assistant",
        ):
            flush_calls()
            content = _responses_content_to_chat(
                item.get("content"), warnings, f"{pointer}/content"
            )
            if content not in (None, "", []):
                messages.append({"role": item["role"], "content": content})
        elif kind in ("function_call", "custom_call", "custom_tool_call"):
            call_kind = "custom" if kind != "function_call" else "function"
            call_id = item.get("call_id") or item.get("id")
            nested = {
                key: item[key] for key in ("name", "arguments", "input") if key in item
            }
            if isinstance(call_id, str) and nested.get("name") is not None:
                pending_calls.append(
                    {"id": call_id, "type": call_kind, call_kind: nested}
                )
            else:
                raise ConversionError(
                    "tool call item is missing call_id or name", param=pointer
                )
        elif kind in (
            "function_call_output",
            "custom_call_output",
            "custom_tool_call_output",
        ):
            flush_calls()
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                raise ConversionError("tool output is missing call_id", param=pointer)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": item.get("output", ""),
                }
            )
        elif kind in ("reasoning", "item_reference"):
            if kind == "item_reference":
                raise ConversionError(
                    "item_reference requires Responses server state", param=pointer
                )
            warnings.add(
                pointer, "dropped", "reasoning history has no Chat message equivalent"
            )
        else:
            warnings.add(pointer, "dropped", "input item has no Chat mapping")
    flush_calls()
    if not messages:
        raise ConversionError("conversion produced no usable messages", param="input")
    target["messages"] = messages
    _warn_unknown(source, used, warnings)
    return target


def convert_request(
    body: bytes,
    source_protocol: str,
    target_protocol: str,
    warnings: ConversionWarnings,
) -> bytes:
    source = _object(body, "request body")
    if source_protocol == PROTOCOL_CHAT and target_protocol == PROTOCOL_RESPONSES:
        return _dump(chat_request_to_responses(source, warnings))
    if source_protocol == PROTOCOL_RESPONSES and target_protocol == PROTOCOL_CHAT:
        return _dump(responses_request_to_chat(source, warnings))
    raise AssertionError("convert_request called for matching protocols")


def _usage_chat_to_responses(
    usage: Any, warnings: ConversionWarnings
) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    target: dict[str, Any] = {}
    names = {
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "prompt_tokens_details": "input_tokens_details",
        "completion_tokens_details": "output_tokens_details",
    }
    for source, destination in names.items():
        if source in usage:
            target[destination] = usage[source]
    for name in usage:
        if name not in names:
            warnings.add(
                f"/usage/{name}", "dropped", "usage field has no Responses mapping"
            )
    if not target:
        return None
    target.setdefault("input_tokens", 0)
    target.setdefault("output_tokens", 0)
    target.setdefault("total_tokens", target["input_tokens"] + target["output_tokens"])
    input_details = target.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    else:
        input_details = dict(input_details)
    input_details.setdefault("cache_write_tokens", 0)
    input_details.setdefault("cached_tokens", 0)
    target["input_tokens_details"] = input_details
    output_details = target.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}
    else:
        output_details = dict(output_details)
    output_details.setdefault("reasoning_tokens", 0)
    target["output_tokens_details"] = output_details
    return target


def _usage_responses_to_chat(
    usage: Any, warnings: ConversionWarnings
) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    target: dict[str, Any] = {}
    names = {
        "input_tokens": "prompt_tokens",
        "output_tokens": "completion_tokens",
        "total_tokens": "total_tokens",
        "input_tokens_details": "prompt_tokens_details",
        "output_tokens_details": "completion_tokens_details",
    }
    for source, destination in names.items():
        if source in usage:
            target[destination] = usage[source]
    for name in usage:
        if name not in names:
            warnings.add(f"/usage/{name}", "dropped", "usage field has no Chat mapping")
    return target or None


def chat_response_to_responses(
    source: dict[str, Any], warnings: ConversionWarnings
) -> dict[str, Any]:
    choices = source.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise UpstreamConversionError("Chat response has no usable choice")
    if len(choices) > 1:
        warnings.add("/choices", "approximated", "only Chat choice 0 is converted")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise UpstreamConversionError("Chat choice has no message")
    output: list[dict[str, Any]] = []
    content_parts: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        content_parts.append(
            {"type": "output_text", "text": content, "annotations": []}
        )
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal:
        content_parts.append({"type": "refusal", "refusal": refusal})
    if content_parts:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": content_parts,
            }
        )
    calls = message.get("tool_calls", [])
    if calls is not None and not isinstance(calls, list):
        raise UpstreamConversionError("Chat tool_calls is not an array")
    for index, call in enumerate(calls or []):
        if not isinstance(call, dict):
            warnings.add(
                f"/choices/0/message/tool_calls/{index}", "dropped", "invalid tool call"
            )
            continue
        kind = call.get("type", "function")
        nested = call.get(kind)
        if kind not in ("function", "custom") or not isinstance(nested, dict):
            warnings.add(
                f"/choices/0/message/tool_calls/{index}",
                "dropped",
                "unsupported tool call",
            )
            continue
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call" if kind == "function" else "custom_tool_call",
                "status": "completed",
                "call_id": call.get("id") or f"call_{uuid.uuid4().hex}",
                **nested,
            }
        )
    if not output:
        raise UpstreamConversionError("Chat response contains no mappable output")
    finish = choice.get("finish_reason")
    if finish in {"stop", "tool_calls", "function_call"}:
        status = "completed"
        incomplete = None
    elif finish == "length":
        status = "incomplete"
        incomplete = {"reason": "max_output_tokens"}
    elif finish == "content_filter":
        status = "incomplete"
        incomplete = {"reason": "content_filter"}
    else:
        raise UpstreamConversionError("Chat response has an unknown finish_reason")
    target: dict[str, Any] = {
        "id": source.get("id") or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": source.get("created") or int(time.time()),
        "status": status,
        "model": source.get("model"),
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    if incomplete is not None:
        target["incomplete_details"] = incomplete
    usage = _usage_chat_to_responses(source.get("usage"), warnings)
    if usage is not None:
        target["usage"] = usage
    return target


def responses_response_to_chat(
    source: dict[str, Any], warnings: ConversionWarnings
) -> dict[str, Any]:
    status = source.get("status")
    output = source.get("output")
    if not isinstance(output, list):
        raise UpstreamConversionError("Responses response has no output array")
    texts: list[str] = []
    refusals: list[str] = []
    calls: list[dict[str, Any]] = []
    message_count = 0
    for index, item in enumerate(output):
        pointer = f"/output/{index}"
        if not isinstance(item, dict):
            warnings.add(pointer, "dropped", "output item is not an object")
            continue
        kind = item.get("type")
        if kind == "message":
            message_count += 1
            parts = item.get("content", [])
            if not isinstance(parts, list):
                continue
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text" and isinstance(
                    part.get("text"), str
                ):
                    texts.append(part["text"])
                elif part.get("type") == "refusal" and isinstance(
                    part.get("refusal"), str
                ):
                    refusals.append(part["refusal"])
                else:
                    warnings.add(
                        f"{pointer}/content/{part_index}",
                        "dropped",
                        "output content has no Chat mapping",
                    )
        elif kind in ("function_call", "custom_tool_call", "custom_call"):
            call_kind = "function" if kind == "function_call" else "custom"
            nested = {
                key: item[key] for key in ("name", "arguments", "input") if key in item
            }
            calls.append(
                {
                    "id": item.get("call_id")
                    or item.get("id")
                    or f"call_{uuid.uuid4().hex}",
                    "type": call_kind,
                    call_kind: nested,
                }
            )
        else:
            warnings.add(pointer, "dropped", "output item has no Chat mapping")
    if message_count > 1:
        warnings.add(
            "/output",
            "approximated",
            "multiple Responses messages are merged into one Chat choice",
        )
    if not texts and not refusals and not calls:
        raise UpstreamConversionError("Responses response contains no mappable output")
    if status == "completed":
        finish = "tool_calls" if calls else "stop"
    elif status == "incomplete":
        reason = (source.get("incomplete_details") or {}).get("reason")
        if reason == "content_filter":
            finish = "content_filter"
        else:
            finish = "length"
            if reason not in (None, "max_output_tokens"):
                warnings.add(
                    "/incomplete_details/reason",
                    "approximated",
                    "unknown incomplete reason becomes length",
                )
    else:
        raise UpstreamConversionError(
            "Responses response is not in a successful terminal state"
        )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(texts) if texts else None,
    }
    if refusals:
        message["refusal"] = "".join(refusals)
    if calls:
        message["tool_calls"] = calls
    target: dict[str, Any] = {
        "id": source.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": source.get("created_at") or int(time.time()),
        "model": source.get("model"),
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish, "logprobs": None}
        ],
    }
    usage = _usage_responses_to_chat(source.get("usage"), warnings)
    if usage is not None:
        target["usage"] = usage
    return target


def convert_response(
    body: bytes,
    source_protocol: str,
    target_protocol: str,
    warnings: ConversionWarnings,
) -> bytes:
    try:
        source = _object(body, "upstream response")
    except ConversionError as exc:
        raise UpstreamConversionError(str(exc)) from exc
    if source_protocol == PROTOCOL_CHAT and target_protocol == PROTOCOL_RESPONSES:
        return _dump(chat_response_to_responses(source, warnings))
    if source_protocol == PROTOCOL_RESPONSES and target_protocol == PROTOCOL_CHAT:
        return _dump(responses_response_to_chat(source, warnings))
    raise AssertionError("convert_response called for matching protocols")


def normalized_error(body: bytes, status: int) -> bytes:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        cleaned = {
            key: error[key]
            for key in ("message", "type", "code", "param")
            if key in error
        }
        if "message" in cleaned:
            return _dump({"error": cleaned})
    return _dump(
        {
            "error": {
                "message": f"upstream returned HTTP {status}",
                "type": "upstream_error",
                "code": "upstream_error",
                "param": None,
            }
        }
    )


def _response_shell(
    response_id: str, model: Any, created_at: int, status: str
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": [],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


class ChatToResponsesStream:
    def __init__(self, warnings: ConversionWarnings) -> None:
        self.warnings = warnings
        self.sequence = 0
        self.started = False
        self.terminal = False
        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.model: Any = None
        self.created_at = int(time.time())
        self.finish_reason: str | None = None
        self.usage: Any = None
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.message_open = False
        self.message_output_index: int | None = None
        self.text_open = False
        self.text = ""
        self.refusal = ""
        self.tools: dict[int, dict[str, Any]] = {}
        self.next_output_index = 0

    def _event(self, kind: str, **fields: Any) -> SseEvent:
        value = {"type": kind, "sequence_number": self.sequence, **fields}
        self.sequence += 1
        return json_event(kind, value)

    def failure(self, message: str) -> SseEvent | None:
        if self.terminal:
            return None
        response = _response_shell(
            self.response_id, self.model, self.created_at, "failed"
        )
        response["error"] = {"code": "server_error", "message": message}
        self.terminal = True
        return self._event("response.failed", response=response)

    def _start(self, chunk: dict[str, Any]) -> list[SseEvent]:
        if self.started:
            return []
        self.started = True
        self.response_id = chunk.get("id") or self.response_id
        self.model = chunk.get("model")
        self.created_at = chunk.get("created") or self.created_at
        created = _response_shell(
            self.response_id, self.model, self.created_at, "in_progress"
        )
        return [
            self._event("response.created", response=created),
            self._event("response.in_progress", response=created),
        ]

    def _open_message(self) -> list[SseEvent]:
        if self.message_open:
            return []
        self.message_open = True
        self.message_output_index = self.next_output_index
        self.next_output_index += 1
        item = {
            "id": self.message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        return [
            self._event(
                "response.output_item.added",
                output_index=self.message_output_index,
                item=item,
            )
        ]

    def _open_tool(self, state: dict[str, Any]) -> list[SseEvent]:
        if state["opened"]:
            return []
        if not state["id"] or not state["name"]:
            return []
        state["opened"] = True
        state["output_index"] = self.next_output_index
        self.next_output_index += 1
        item_type = (
            "function_call" if state["kind"] == "function" else "custom_tool_call"
        )
        item = {
            "id": f"fc_{uuid.uuid4().hex}",
            "type": item_type,
            "status": "in_progress",
            "call_id": state["id"],
            "name": state["name"],
            "arguments" if state["kind"] == "function" else "input": "",
        }
        state["item"] = item
        result = [
            self._event(
                "response.output_item.added",
                output_index=state["output_index"],
                item=item,
            )
        ]
        if state["arguments"]:
            event_type = (
                "response.function_call_arguments.delta"
                if state["kind"] == "function"
                else "response.custom_tool_call_input.delta"
            )
            result.append(
                self._event(
                    event_type,
                    item_id=item["id"],
                    output_index=state["output_index"],
                    delta=state["arguments"],
                )
            )
            state["emitted_arguments"] = len(state["arguments"])
        return result

    def feed(self, event: SseEvent) -> list[SseEvent]:
        if event.data == b"[DONE]":
            return self.finish()
        if not event.data:
            return []
        try:
            chunk = event.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise UpstreamConversionError("invalid Chat SSE JSON") from exc
        result = self._start(chunk)
        if chunk.get("model") is not None:
            self.model = chunk["model"]
        if chunk.get("usage") is not None:
            self.usage = chunk["usage"]
        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            raise UpstreamConversionError("Chat SSE choices is not an array")
        selected = [
            choice
            for choice in choices
            if isinstance(choice, dict) and choice.get("index", 0) == 0
        ]
        if any(
            isinstance(choice, dict) and choice.get("index", 0) != 0
            for choice in choices
        ):
            self.warnings.add(
                "/choices", "approximated", "only Chat choice 0 is converted"
            )
        if not selected:
            return result
        choice = selected[0]
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise UpstreamConversionError("Chat SSE delta is not an object")
        content = delta.get("content")
        if isinstance(content, str) and content:
            result.extend(self._open_message())
            assert self.message_output_index is not None
            if not self.text_open:
                self.text_open = True
                part = {"type": "output_text", "text": "", "annotations": []}
                result.append(
                    self._event(
                        "response.content_part.added",
                        item_id=self.message_id,
                        output_index=self.message_output_index,
                        content_index=0,
                        part=part,
                    )
                )
            self.text += content
            result.append(
                self._event(
                    "response.output_text.delta",
                    item_id=self.message_id,
                    output_index=self.message_output_index,
                    content_index=0,
                    delta=content,
                )
            )
        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            result.extend(self._open_message())
            assert self.message_output_index is not None
            self.refusal += refusal
            result.append(
                self._event(
                    "response.refusal.delta",
                    item_id=self.message_id,
                    output_index=self.message_output_index,
                    content_index=1 if self.text_open else 0,
                    delta=refusal,
                )
            )
        tool_calls = delta.get("tool_calls", [])
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise UpstreamConversionError("Chat SSE tool_calls is not an array")
        for call in tool_calls or []:
            if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                continue
            index = call["index"]
            kind = call.get("type", "function")
            if kind not in ("function", "custom"):
                self.warnings.add(
                    "/stream/tool_calls", "dropped", "unsupported streaming tool call"
                )
                continue
            state = self.tools.setdefault(
                index,
                {
                    "id": None,
                    "kind": kind,
                    "name": "",
                    "arguments": "",
                    "emitted_arguments": 0,
                    "opened": False,
                    "output_index": None,
                },
            )
            if call.get("id") is not None:
                state["id"] = call["id"]
            nested = call.get("function") or call.get("custom") or {}
            arguments: str | None = None
            if isinstance(nested, dict):
                if isinstance(nested.get("name"), str):
                    state["name"] += nested["name"]
                arguments = nested.get("arguments", nested.get("input"))
                if isinstance(arguments, str):
                    state["arguments"] += arguments
            if not state["opened"] and arguments:
                result.extend(self._open_tool(state))
            elif state["opened"] and isinstance(arguments, str) and arguments:
                state["emitted_arguments"] += len(arguments)
                event_type = (
                    "response.function_call_arguments.delta"
                    if state["kind"] == "function"
                    else "response.custom_tool_call_input.delta"
                )
                result.append(
                    self._event(
                        event_type,
                        item_id=state["item"]["id"],
                        output_index=state["output_index"],
                        delta=arguments,
                    )
                )
        if choice.get("finish_reason") is not None:
            self.finish_reason = choice["finish_reason"]
        return result

    def finish(self) -> list[SseEvent]:
        if self.terminal:
            return []
        if not self.started or self.finish_reason is None:
            raise UpstreamConversionError(
                "Chat stream ended without a terminal finish_reason"
            )
        result: list[SseEvent] = []
        indexed_output: list[tuple[int, dict[str, Any]]] = []
        if self.message_open:
            assert self.message_output_index is not None
            content: list[dict[str, Any]] = []
            content_index = 0
            if self.text_open:
                result.append(
                    self._event(
                        "response.output_text.done",
                        item_id=self.message_id,
                        output_index=self.message_output_index,
                        content_index=content_index,
                        text=self.text,
                    )
                )
                part = {"type": "output_text", "text": self.text, "annotations": []}
                result.append(
                    self._event(
                        "response.content_part.done",
                        item_id=self.message_id,
                        output_index=self.message_output_index,
                        content_index=content_index,
                        part=part,
                    )
                )
                content.append(part)
                content_index += 1
            if self.refusal:
                result.append(
                    self._event(
                        "response.refusal.done",
                        item_id=self.message_id,
                        output_index=self.message_output_index,
                        content_index=content_index,
                        refusal=self.refusal,
                    )
                )
                part = {"type": "refusal", "refusal": self.refusal}
                result.append(
                    self._event(
                        "response.content_part.done",
                        item_id=self.message_id,
                        output_index=self.message_output_index,
                        content_index=content_index,
                        part=part,
                    )
                )
                content.append(part)
            message = {
                "id": self.message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
            result.append(
                self._event(
                    "response.output_item.done",
                    output_index=self.message_output_index,
                    item=message,
                )
            )
            indexed_output.append((self.message_output_index, message))
        for index in sorted(self.tools):
            state = self.tools[index]
            if not state.get("opened"):
                result.extend(self._open_tool(state))
            if not state.get("opened"):
                raise UpstreamConversionError(
                    "Chat stream ended with an incomplete tool call"
                )
            value_key = "arguments" if state["kind"] == "function" else "input"
            item = {
                **state["item"],
                "status": "completed",
                value_key: state["arguments"],
            }
            done_type = (
                "response.function_call_arguments.done"
                if state["kind"] == "function"
                else "response.custom_tool_call_input.done"
            )
            result.append(
                self._event(
                    done_type,
                    item_id=item["id"],
                    output_index=state["output_index"],
                    **{value_key: state["arguments"]},
                )
            )
            result.append(
                self._event(
                    "response.output_item.done",
                    output_index=state["output_index"],
                    item=item,
                )
            )
            indexed_output.append((state["output_index"], item))
        if not indexed_output:
            raise UpstreamConversionError("Chat stream contains no mappable output")
        if self.finish_reason in {"stop", "tool_calls", "function_call"}:
            status = "completed"
            terminal_type = "response.completed"
            incomplete = None
        elif self.finish_reason in {"length", "content_filter"}:
            status = "incomplete"
            terminal_type = "response.incomplete"
            incomplete = {
                "reason": "max_output_tokens"
                if self.finish_reason == "length"
                else "content_filter"
            }
        else:
            raise UpstreamConversionError("Chat stream has an unknown finish_reason")
        response = _response_shell(
            self.response_id, self.model, self.created_at, status
        )
        response["output"] = [
            item for _index, item in sorted(indexed_output, key=lambda pair: pair[0])
        ]
        usage = _usage_chat_to_responses(self.usage, self.warnings)
        if usage is not None:
            response["usage"] = usage
        if incomplete is not None:
            response["incomplete_details"] = incomplete
        result.append(self._event(terminal_type, response=response))
        self.terminal = True
        return result


class ResponsesToChatStream:
    def __init__(self, warnings: ConversionWarnings, *, include_usage: bool) -> None:
        self.warnings = warnings
        self.include_usage = include_usage
        self.response_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.model: Any = None
        self.created = int(time.time())
        self.role_emitted = False
        self.terminal = False
        self.next_tool_index = 0
        self.tools: dict[str, dict[str, Any]] = {}
        self.tools_by_output: dict[int, dict[str, Any]] = {}

    def _chunk(
        self, delta: dict[str, Any], *, finish_reason: str | None = None
    ) -> SseEvent:
        return json_event(
            None,
            {
                "id": self.response_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                        "logprobs": None,
                    }
                ],
            },
        )

    def _role(self) -> list[SseEvent]:
        if self.role_emitted:
            return []
        self.role_emitted = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def feed(self, event: SseEvent) -> list[SseEvent]:
        if not event.data:
            return []
        try:
            value = event.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise UpstreamConversionError("invalid Responses SSE JSON") from exc
        kind = value.get("type") or event.event
        result: list[SseEvent] = []
        if kind in ("response.created", "response.in_progress"):
            response = value.get("response") or {}
            if isinstance(response, dict):
                self.response_id = response.get("id") or self.response_id
                self.model = response.get("model", self.model)
                self.created = response.get("created_at") or self.created
        elif kind == "response.output_text.delta" and isinstance(
            value.get("delta"), str
        ):
            result.extend(self._role())
            result.append(self._chunk({"content": value["delta"]}))
        elif kind == "response.refusal.delta" and isinstance(value.get("delta"), str):
            result.extend(self._role())
            result.append(self._chunk({"refusal": value["delta"]}))
        elif kind == "response.output_item.added":
            item = value.get("item")
            if isinstance(item, dict) and item.get("type") in (
                "function_call",
                "custom_tool_call",
                "custom_call",
            ):
                result.extend(self._role())
                item_id = item.get("id") or f"item_{uuid.uuid4().hex}"
                tool_kind = (
                    "function" if item.get("type") == "function_call" else "custom"
                )
                state = {
                    "index": self.next_tool_index,
                    "kind": tool_kind,
                    "id": item.get("call_id") or item_id,
                    "name": item.get("name", ""),
                    "arguments": "",
                }
                self.next_tool_index += 1
                self.tools[item_id] = state
                if isinstance(value.get("output_index"), int):
                    self.tools_by_output[value["output_index"]] = state
                nested = {
                    "name": state["name"],
                    "arguments" if tool_kind == "function" else "input": "",
                }
                result.append(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": state["index"],
                                    "id": state["id"],
                                    "type": tool_kind,
                                    tool_kind: nested,
                                }
                            ]
                        }
                    )
                )
        elif kind in (
            "response.function_call_arguments.delta",
            "response.custom_tool_call_input.delta",
        ) and isinstance(value.get("delta"), str):
            item_id = value.get("item_id")
            output_index = value.get("output_index")
            state = self.tools.get(item_id) if isinstance(item_id, str) else None
            if state is None and isinstance(output_index, int):
                state = self.tools_by_output.get(output_index)
            if state is None:
                self.warnings.add(
                    "/stream/item_id",
                    "dropped",
                    "tool argument delta references an unknown item",
                )
            else:
                state["arguments"] += value["delta"]
                nested_key = "arguments" if state["kind"] == "function" else "input"
                result.append(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": state["index"],
                                    "type": state["kind"],
                                    state["kind"]: {nested_key: value["delta"]},
                                }
                            ]
                        }
                    )
                )
        elif kind in (
            "response.function_call_arguments.done",
            "response.custom_tool_call_input.done",
            "response.output_item.done",
        ):
            item = value.get("item") if kind == "response.output_item.done" else None
            item_id = value.get("item_id") or (
                item.get("id") if isinstance(item, dict) else None
            )
            output_index = value.get("output_index")
            state = self.tools.get(item_id) if isinstance(item_id, str) else None
            if state is None and isinstance(output_index, int):
                state = self.tools_by_output.get(output_index)
            full = value.get("arguments", value.get("input"))
            if full is None and isinstance(item, dict):
                full = item.get("arguments", item.get("input"))
            if (
                state is not None
                and isinstance(full, str)
                and full.startswith(state["arguments"])
            ):
                suffix = full[len(state["arguments"]) :]
                if suffix:
                    state["arguments"] = full
                    nested_key = "arguments" if state["kind"] == "function" else "input"
                    result.append(
                        self._chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": state["index"],
                                        "type": state["kind"],
                                        state["kind"]: {nested_key: suffix},
                                    }
                                ]
                            }
                        )
                    )
        elif kind in ("response.completed", "response.incomplete"):
            response = value.get("response") or {}
            if not isinstance(response, dict):
                raise UpstreamConversionError(
                    "Responses terminal event has no response"
                )
            status = response.get("status")
            if status == "completed":
                finish = "tool_calls" if self.tools else "stop"
            elif status == "incomplete":
                reason = (response.get("incomplete_details") or {}).get("reason")
                finish = "content_filter" if reason == "content_filter" else "length"
            else:
                raise UpstreamConversionError(
                    "Responses stream has an invalid terminal status"
                )
            result.extend(self._role())
            result.append(self._chunk({}, finish_reason=finish))
            usage = _usage_responses_to_chat(response.get("usage"), self.warnings)
            if self.include_usage and usage is not None:
                result.append(
                    json_event(
                        None,
                        {
                            "id": self.response_id,
                            "object": "chat.completion.chunk",
                            "created": self.created,
                            "model": self.model,
                            "choices": [],
                            "usage": usage,
                        },
                    )
                )
            result.append(SseEvent(data=b"[DONE]"))
            self.terminal = True
        elif kind == "response.failed":
            raise UpstreamConversionError("Responses stream failed upstream")
        elif kind in (
            "response.content_part.added",
            "response.content_part.done",
            "response.output_text.done",
        ):
            pass
        elif isinstance(kind, str):
            self.warnings.add(
                "/stream/type",
                "dropped",
                "Responses event type has no Chat chunk mapping",
            )
        return result

    def finish(self) -> list[SseEvent]:
        if not self.terminal:
            raise UpstreamConversionError(
                "Responses stream ended without a terminal event"
            )
        return []
