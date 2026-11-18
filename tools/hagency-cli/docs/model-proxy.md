# Local model proxy

[CLI guide](../README.md) | [Configuration](configuration.md) | [Development](development.md)

`hgc service model-proxy start` starts a background process that exposes both OpenAI Responses and Chat Completions interfaces for every configured provider. Provider selection comes from the URL, never from `model`; the model value is forwarded unchanged.

Create `hagency-model-proxy.toml` beside `hagency-config.toml`:

```toml
version = 1
default_provider = "openai"

[providers.openai]
adapter = "openai"
api_key = { env = "OPENAI_API_KEY" }

[providers.corp]
adapter = "openai_compatible"
base_url = "https://llm.corp.example/openai/v1"
hook = "corp.py"

[providers.corp.headers]
"X-Tenant" = { env = "CORP_TENANT" }
```

Environment-backed values are resolved from `.env` beside `hagency-model-proxy.toml` and then the process environment, with the process environment taking precedence. The same merged, read-only mapping is available to trusted Hooks as `init.env`, so provider-specific authentication can consume workspace credentials without loading files itself. Keep `.env` out of version control.

Then point an OpenAI-compatible client at one of these base URLs:

```text
http://127.0.0.1:8765/v1
http://127.0.0.1:8765/openai/v1
http://127.0.0.1:8765/corp/v1
```

Manage the background process with the same workspace root or explicit config path:

```sh
hgc service model-proxy start -r <workspace>
hgc service model-proxy stop -r <workspace>
hgc service model-proxy restart -r <workspace>
```

Linux uses a detached session; Windows uses a detached, no-console process group. On Windows, macOS, Linux, and WSL, state and logs are stored below `$XDG_STATE_HOME/hagency/model-proxy/<config-hash>/`. If `XDG_STATE_HOME` is unset, empty, or relative, the base is `~/.local/state/hagency`. This includes `state.json`, `control.lock`, `service.log`, and the `debug/` directory. `start` reports the exact log path; logs rotate at 10 MiB with three backups.

Access records in `service.log` contain the HTTP method, a credential-redacted
path/query, response status, and elapsed seconds. This format and URL redaction
apply with debug tracing enabled or disabled.

The bare `/v1` routes use `default_provider`; `/<provider>/v1` selects one explicitly. `POST /responses`, `POST /chat/completions`, and `GET /models` are available without per-client configuration. A matching upstream protocol uses a raw entity path; the other interface is converted. `/models` proxies the adapter's model-list operation and uses the same request, authentication, and response Hook stages. A Hook may instead implement `fetch_models(ctx)` and return model ID strings when the provider has no standard model-list endpoint; because that compact contract contains no creation metadata, synthesized model records use `created = 0` as a stable unknown value. Additional resource operations under the native protocol family are passed through without cross-protocol emulation.

Downstream credential headers are stripped by default. Use `forward_credential_headers` for explicit per-provider forwarding, static/env headers for normal authentication, or a trusted Python hook under `<config-dir>/hooks/` for custom request, signing, and response handling. Hooks receive the merged environment as the read-only `init.env` mapping, run in-process, and take effect after a restart. Defining `process_response` buffers non-SSE responses before invoking the hook and enforces a 64 MiB response limit; omit that method when response inspection is unnecessary. The server only accepts loopback listen addresses.

`adapter = "openai"` supplies the Responses protocol and OpenAI API root; `adapter = "openai_compatible"` defaults to Chat Completions and requires `base_url`. Override `protocol` at provider level when needed. To add a provider family, add one module under [`model_proxy/providers`](../src/hagency_cli/model_proxy/providers/README.md) that exports `ADAPTER`; the filename becomes the adapter value and no central registry change is needed.

An explicit `--config /path/to/hagency-model-proxy.toml` needs no Hagency workspace and is mutually exclusive with `--root`. Relative config paths use the invocation directory. Use the same resolved config path and state directory for lifecycle commands. `start` and `restart` return after the worker binds its port; starting an already running service fails. Stopping an already stopped worker succeeds. Restart defaults to `127.0.0.1:8765` on each invocation, so repeat custom `--host` and `--port` settings. A failed restart can leave the old worker stopped; read the reported log for diagnosis.

## Debug traces

Enable detailed HTTP tracing for a worker with `hgc service model-proxy start --debug`
or `hgc service model-proxy restart --debug`. To enable it through configuration,
put `debug = true` at the top level of `hagency-model-proxy.toml`, before any
`[providers.*]` tables:

```toml
version = 1
debug = true
default_provider = "openai"

[providers.openai]
adapter = "openai"
api_key = { env = "OPENAI_API_KEY" }
```

Either the flag or the configuration value enables tracing; both default to
false. The flag does not modify TOML or carry over to the next restart. To disable
tracing, set `debug = false` (or remove it) and restart without `--debug`.

Startup reports the effective debug state and the exact debug directory, beside
`service.log`. Each provider has a continuously appended UTF-8 JSONL file named
`provider-<name-prefix>-<hash>.jsonl`; the hash distinguishes names on
case-insensitive filesystems. Unknown providers and unmatched routes share
`unrouted.jsonl`. Each file rotates **during operation** at 10 MiB, retaining three
backups (`.1` is newest). Bodies are not truncated, but older records are removed
by rotation, so a large or long-running exchange can span backups or outlive the
retention window. Ordinary `service.log` retains its existing startup-time rotation.

Records share a `request_id` with Hook contexts and conversion warnings. The four
`stage` values are `client_request`, `upstream_request`, `upstream_response`, and
`client_response`. `start`, `headers`, `body`, and `end` events capture method, URL,
status, duplicate headers, body chunks, byte counts, and elapsed milliseconds as
applicable. The final `exchange` event reports whether an upstream was called and
the completion reason. A Hook-generated model list or local rejection has no
invented upstream exchange. An unread or interrupted body ends with
`complete: false`.

The upstream stages capture content before response Hooks/conversion and after
request Hooks/authentication respectively. This includes `/models`, native
resource operations, errors, and SSE. Streams are recorded incrementally without
buffering the entire response. Each body record carries `sequence`, `size`,
`encoding`, and `body`: encode `utf-8` strings or decode `base64` strings, then
concatenate in sequence order for a request and stage to reconstruct the bytes.
Logging splits chunks at 64 KiB without changing HTTP chunking; compressed and
non-UTF-8 content remains lossless.

Trace records pass through a bounded queue to a background writer for JSON
serialization, file writes, flushes, and rotation. A slow disk can make traced
requests wait asynchronously for queue capacity; disk I/O does not block the
HTTP event loop. Order is preserved, records are not dropped on queue saturation,
and graceful application shutdown drains the queue before closing files.

Credential headers, Cookie/Set-Cookie, sensitive URL query parameters, and headers
or query parameters set by authentication Hooks are redacted in log metadata.
Access logs also redact sensitive URL parameters. Redaction affects log copies
only. **Bodies retain their full business content**, including prompts, tool data,
and any secrets embedded in that content. Debug logging does not dump environment
variables or configuration files.

Debug file initialization failures prevent startup with an error in `service.log`.
A file write failure disables the affected debug file. An unexpected failure in
the background writer disables tracing for the running worker. Both report an
error to `service.log` and allow requests to continue. Disabled debug mode
creates no debug files.

## Provider hooks

A hook file named by `hook = "corp.py"` lives under `<config-dir>/hooks/`. It exports a `Hook` class that accepts `HookInit`. For a provider using a custom credential header, a minimal `hooks/corp.py` can be:

```python
from hagency_cli.model_proxy import AuthPatch, HeaderPatch


class Hook:
    def __init__(self, init):
        self.token = init.env["CORP_TOKEN"]

    async def authenticate(self, ctx, request):
        return AuthPatch(
            headers=HeaderPatch(set=(("X-Corp-Token", self.token),))
        )
```

`prepare_request` and `process_response` see provider-native data. `authenticate` sees the final request body and may return only header/query patches. A hook can implement `fetch_models(ctx)` returning `list[str]` to supply model IDs; missing methods preserve the relevant raw path. Matching-protocol request/response entity bytes remain intact except where explicitly configured body or SSE hooks modify them. Cross-protocol requests use the built-in bridge.

Hooks are trusted in-process code. Loading and contract validation happen before listening; failures are fail-closed, and files are not hot-reloaded. Keep deployment secrets in environment-backed configuration and keep model routing out of provider adapters. The public hook types remain in `hagency_cli.model_proxy`; provider adapter details are in the [provider guide](../src/hagency_cli/model_proxy/providers/README.md).
