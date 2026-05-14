# Log Analysis Patterns

Optional examples for concrete tooling. Prefer the user's available log viewer, cloud console, editor, notebook, terminal, or scripting environment.

## Source Discovery and Sampling

- Preserve: source name/path, modified time, size, approximate line/record count, rotation/compression, raw ordering, timestamp style, and record shape.
- Approach: start from the user-provided source; otherwise check repo/runtime config, CI artifacts, observability links, and common log locations. Estimate volume, then sample a few records before filtering.

```powershell
Get-ChildItem -Recurse -File -Include *.log,*.jsonl,*.ndjson,*.gz | Select FullName,Length,LastWriteTime
(Get-Content .\app.log | Measure-Object -Line).Lines
Get-Content .\app.log -TotalCount 10
Get-Content .\app.log -Tail 100
```

```sh
find . -type f \( -name '*.log' -o -name '*.jsonl' -o -name '*.ndjson' -o -name '*.gz' \)
wc -l app.log
sed -n '1,10p' app.log
gzip -cd app.log.gz | sed -n '1,10p'
```

```python
from pathlib import Path
for p in Path(".").rglob("*"):
    if p.is_file() and (p.suffix in {".log", ".jsonl", ".ndjson", ".gz"} or "log" in p.name.lower()):
        print(p, p.stat().st_size)
print(sum(1 for _ in open("app.log", errors="replace")))
```

## Time Windows

- Preserve: requested time zone, observed range, boundary event, deploy/config time, and skipped records.
- Approach: use native time filters when available; otherwise filter by sortable timestamp prefixes or parse records with a structured tool.

```text
Log query shape:
service = "<service>"
timestamp between <start> and <end>
level in ("error", "warn")
```

```powershell
Select-String -Path .\app.log -Pattern "2026-05-14T09:3","2026-05-14T09:4","2026-05-14T10:0"
```

```sh
awk '$0 >= "2026-05-14T09:30:00" && $0 <= "2026-05-14T10:15:00"' app.log
```

## Repeated Failures

- Preserve: count, normalized message, first/last seen time, source, and one redacted example.
- Approach: search failure terms, normalize volatile values, group repeated shapes, then inspect examples from the largest groups.

```powershell
Select-String -Path .\app.log -Pattern "error","exception","fatal","failed","timeout" -CaseSensitive:$false | Select -First 80
```

```sh
rg -n -i '\b(error|exception|fatal|failed|timeout)\b' app.log | head -80
rg -i '\b(error|exception|fatal|failed|timeout)\b' app.log | sed -E 's/[0-9a-fA-F-]{32,}/<ID>/g; s/[0-9]{4,}/<NUM>/g' | sort | uniq -c | sort -nr | head -20
```

```python
import re
from collections import Counter
pat = re.compile(r"\b(error|exception|fatal|failed|timeout)\b", re.I)
counts = Counter(re.sub(r"[0-9a-fA-F-]{32,}|\b\d{4,}\b", "<ID>", l.strip()) for l in open("app.log", errors="replace") if pat.search(l))
for msg, n in counts.most_common(20):
    print(n, msg[:240])
```

## JSONL Records

- Preserve: timestamp, level, message, status, duration, path/job, request ID, trace ID, and source.
- Approach: prefer field selection over text matching; if records are malformed, fall back to text search and mention that limit.

```powershell
Get-Content .\app.jsonl | % { $_ | ConvertFrom-Json } | ? { $_.level -match "error|fatal|critical" } | Select timestamp,level,message,request_id,trace_id
```

```sh
jq -r 'select(((.level // .severity // "") | ascii_downcase) | test("error|fatal|critical")) | [.timestamp // .time, .level // .severity, .message // .msg, .request_id // .trace_id] | @tsv' app.jsonl
jq -r 'select((.duration_ms // .latency_ms // .elapsed_ms // 0) >= 1000) | [.timestamp // .time, .duration_ms // .latency_ms // .elapsed_ms, .path // .job] | @tsv' app.jsonl
```

```python
import json
for line in open("app.jsonl", errors="replace"):
    r = json.loads(line)
    if str(r.get("level", r.get("severity", ""))).lower() in {"error", "fatal", "critical"}:
        print(r.get("timestamp") or r.get("time"), r.get("message") or r.get("msg"))
```

## Access and Performance Patterns

- Preserve: status, method, route/job, host/IP, duration, timestamp, source, and correlation IDs.
- Approach: count statuses/routes first; strip query strings for aggregate route counts; inspect slowest or most changed groups.

```powershell
Select-String -Path .\access.log -Pattern '" [45][0-9][0-9] ' | Select -First 50
Select-String -Path .\app.log -Pattern "duration_ms","latency_ms","elapsed_ms","response_time" -CaseSensitive:$false
```

```sh
awk '{print $9}' access.log | sort | uniq -c | sort -nr
awk -F'"' '{split($2, r, " "); print r[2]}' access.log | sed 's/[?].*$//' | sort | uniq -c | sort -nr | head -20
rg -n -i 'duration(_ms)?[=:] ?[0-9]+|latency(_ms)?[=:] ?[0-9]+|elapsed(_ms)?[=:] ?[0-9]+' app.log
```

```text
Query shape:
group by status, route
show count(), p95(duration), max(duration)
```

## Platform Logs

- Preserve: service, environment, namespace/project, time range, host/pod/task, and filters used.
- Approach: use native platform time filters and field selectors; keep exports small.

```sh
docker logs --since 30m --tail 200 <name>
docker compose logs --since 30m --tail 200 <service>
kubectl logs <pod> --since=30m --tail=200
journalctl -u <service> --since "30 minutes ago" --no-pager
```

```text
Cloud/observability query:
service = "<service>"
environment = "<env>"
timestamp between <start> and <end>
```

## Redaction and Report Shape

- Redact: authorization headers, cookies, tokens, passwords, API keys, session IDs, query-string credentials, and long secret-like values.
- Preserve: diagnostic shape, timestamp, source, level, route/job, status, duration, and safe correlation IDs.

```powershell
Get-Content .\app.log | % { $_ -replace '(?i)(password|token|secret|api[-_]?key|session|cookie)(\s*[:=]\s*)[^,\s]+', '$1$2[REDACTED]' -replace '(?i)(Authorization:\s*Bearer\s+)\S+', '$1[REDACTED]' } | Select -First 40
```

```sh
sed -E 's/([?&](password|token|secret|api_key|session|cookie)=)[^&"}),[:space:]]+/\1[REDACTED]/Ig; s/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig; s/((password|token|secret|api[-_]?key|session|cookie)[=:])[^,[:space:]]+/\1[REDACTED]/Ig'
```

```text
Summary: what changed, where, and when.
Evidence: counts, sources, time range, statuses, endpoints/jobs, hosts/IPs, IDs.
Samples: 3-8 representative redacted records.
Next checks: service, deploy, dashboard, trace, query, or code path.
Limits: missing logs, broad window, invalid records, missing rotations, unverified hypothesis.
```
