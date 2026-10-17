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

Linux uses a detached session; Windows uses a detached, no-console process group. State and logs are stored below `XDG_STATE_HOME`/`~/.local/state` on Linux and `LOCALAPPDATA` on Windows. Set `HAGENCY_STATE_HOME` to an absolute path to override that root. `start` reports the exact log path; logs rotate at 10 MiB with three backups.

The bare `/v1` routes use `default_provider`; `/<provider>/v1` selects one explicitly. `POST /responses`, `POST /chat/completions`, and `GET /models` are available without per-client configuration. A matching upstream protocol uses a raw entity path; the other interface is converted. `/models` proxies the adapter's model-list operation and uses the same request, authentication, and response Hook stages. A Hook may instead implement `fetch_models(ctx)` and return model ID strings when the provider has no standard model-list endpoint; because that compact contract contains no creation metadata, synthesized model records use `created = 0` as a stable unknown value. Additional resource operations under the native protocol family are passed through without cross-protocol emulation.

Downstream credential headers are stripped by default. Use `forward_credential_headers` for explicit per-provider forwarding, static/env headers for normal authentication, or a trusted Python hook under `<config-dir>/hooks/` for custom request, signing, and response handling. Hooks receive the merged environment as the read-only `init.env` mapping, run in-process, and take effect after a restart. Defining `process_response` buffers non-SSE responses before invoking the hook and enforces a 64 MiB response limit; omit that method when response inspection is unnecessary. The server only accepts loopback listen addresses.

`adapter = "openai"` supplies the Responses protocol and OpenAI API root; `adapter = "openai_compatible"` defaults to Chat Completions and requires `base_url`. Override `protocol` at provider level when needed. To add a provider family, add one module under [`model_proxy/providers`](../src/hagency_cli/model_proxy/providers/README.md) that exports `ADAPTER`; the filename becomes the adapter value and no central registry change is needed.

An explicit `--config /path/to/hagency-model-proxy.toml` needs no Hagency workspace and is mutually exclusive with `--root`. Relative config paths use the invocation directory. Use the same resolved config path and state directory for lifecycle commands. `start` and `restart` return after the worker binds its port; starting an already running service fails. Stopping an already stopped worker succeeds. Restart defaults to `127.0.0.1:8765` on each invocation, so repeat custom `--host` and `--port` settings. A failed restart can leave the old worker stopped; read the reported log for diagnosis.

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
