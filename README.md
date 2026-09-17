# Switchyard gateway

A small Python gateway combining NVIDIA Switchyard tier routing with Headroom
structural compression. One container, JSONC configuration, JSON logs. No LiteLLM
package or server, Postgres, UI, or compression sidecar.

```text
Chat Completions client
  → Switchyard selects capable / efficient from original history
  → gateway protects instructions, cached content, and live tool exchanges
  → Headroom compresses eligible older history
  → round-robin selects a replica of the chosen model
  → backend response streams to the client
```

Python 3.14 is required. Exact dependencies are `nemo-switchyard==0.2.0` and
`headroom-ai==0.37.0`; transitive dependencies are locked in `uv.lock`.
Headroom excludes its LiteLLM dependency on Python 3.14. CI checks that LiteLLM
is absent. The gateway installs neither project's optional server/proxy/ML extras.

## Quickstart

```bash
cp .env.example .env
cp config.example.jsonc config.jsonc
# Fill in .env credentials/endpoints and config.jsonc backend model IDs.
docker compose up -d --build --wait
curl -fsS http://127.0.0.1:4000/health/readiness
```

The example config listens on `0.0.0.0` inside the container; Compose publishes
only on host loopback by default. Set `GATEWAY_IP`/`GATEWAY_PORT` in `.env` to change
that binding. Serve public traffic behind your own TLS proxy.

```bash
curl -i http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"switchyard","messages":[{"role":"user","content":"Say hello"}]}'
```

Export `GATEWAY_API_KEY` in your shell for the curl example; Compose's `.env` does
not export it into the shell. Never commit `.env` or `config.jsonc`.

For local development, use `uv sync --frozen --group dev`, export the variables
referenced by your config, and run `uv run switchyard-gateway --config config.jsonc`.
The CLI does not automatically load `.env`. Set `server.host` to `127.0.0.1` for a
local-only process. `--check` validates configuration without starting the server.

## Configuration

`config.example.jsonc` is the configuration reference. Comments and trailing
commas are accepted; duplicate keys and unknown fields are rejected. Environment
references use `{"env":"VARIABLE_NAME"}` as a complete value. Missing or empty
referenced variables fail startup without printing values. Configuration changes
require restart/recreation.

- `server`: required bearer key, host, port, and maximum request size.
- `models`: reusable public labels, backend `model_id`, context window, request
  defaults, and named replica endpoints. `base_url` includes `/v1`; the gateway
  appends `/chat/completions`. Omit `api_key` for an unauthenticated local backend.
- `pairs`: public route labels referencing `capable` and `efficient` models.
  Add any number of pairs without numbered environment variables. Pairs may
  share model definitions. A direct model label bypasses Switchyard.
- `stage`: shared routing policy. Defaults preserve cheap-first behavior and
  escalation/system-prompt settings from the former deployment.
- `compression`: enabled by default. `workers` bounds running plus queued
  submissions; one execution thread protects Headroom's shared mutable pipeline.
  ML compression is always disabled.
- `connect_timeout`, `read_timeout`: per-operation seconds. There is no automatic
  replay after an ambiguous timeout or after streaming begins.
- `cooldown_seconds`: 30 by default. Replica failures respect longer `Retry-After`
  values. Cooldowns and round-robin positions are process-local; run one worker.
  `/health/readiness` consults this cooldown table, so it forgets failures across
  restarts just like routing does.
- `forward_headers`: explicit application `x-` headers, initially `x-session-id`
  and `x-opencode-session`. Backend credentials replace gateway authorization.

Model defaults are merged first; client request fields override them. The selected
backend model ID always wins. Provider-specific fields are forwarded without
LiteLLM translation: put `reasoning_effort` at the top level if your backend expects
it there. This gateway does not invent capability metadata or enforce context
limits; `context_window` supplies Headroom's compression context.

Replica selection happens after tier routing. Connections that cannot be
established and HTTP 429/502/503/504 cool down the endpoint and allow one attempt
on a different eligible replica of the same model. Other failures are not retried.
If no alternative exists, an upstream error status is retained; if no endpoint is
eligible at selection time, the gateway returns 503. Upstream error bodies are
replaced with a sanitized error code. Tier selection never changes for failover.

## Client interface

- `POST /v1/chat/completions`: text and tool messages, regular JSON or SSE streaming.
- `GET /v1/models`: pair labels and direct model labels.
- `GET /health/liveliness`: unauthenticated static process check, always `200
  {"status":"ok"}`.
- `GET /health/readiness`: unauthenticated serving check. Returns `200
  {"status":"ok"}` while at least one configured replica is eligible to serve
  (not cooling down) and `503 {"status":"not_ready"}` when every replica is
  cooling down. It reflects request-routing eligibility observed from chat
  traffic; it does not probe backends, and a never-failed backend is reported
  ready.

Responses keep the requested model alias. Headers `x-request-id`,
`x-gateway-model`, and `x-gateway-endpoint` identify the request, selected model
label, and replica label. Unknown request extension fields and tool schemas are
preserved. Native Responses/Anthropic APIs and multimodal content are not supported.

Compression sees only eligible older history. System/developer instructions,
cache-marked messages, the latest user/assistant exchange, and connected live tool
calls/results remain intact. Compression modifies a private outgoing copy, never
client history. `x-headroom-bypass: true` bypasses it for one request. Short or
fully protected requests may save no tokens. Compression errors continue with
uncompressed messages and a `failed_unknown` log outcome.

Streaming is forwarded incrementally with tool deltas and usage chunks intact.
A completed stream ends with exactly one `data: [DONE]` frame. An interrupted or
malformed upstream stream instead ends with exactly one sanitized terminal frame
`data: {"error":{"message":"upstream_stream_interrupted","type":"gateway_error","code":"upstream_stream_interrupted"}}`
and never a `[DONE]`, so clients must treat a missing `[DONE]` as truncation;
the exchange is logged as interrupted and never replayed. A client disconnect
closes the backend response. The maximum buffered request is configurable; a
single SSE event or nonstream response is limited to 32 MiB.

## Observability

```bash
docker compose logs -f gateway
```

JSON records include request ID, route/tier/model/endpoint labels, status, attempts,
routing/compression/header timings, first upstream body-byte latency, total duration,
compression outcome and token savings, and provider token usage when supplied.
Body-byte latency is not necessarily first-token latency. Token counts from
compression are estimates over eligible history; provider usage is reported
separately. Streams log completed, interrupted, or cancelled outcomes.

`GET /v1/models` emits one `request` record per call: `outcome:"completed"` with
status 200 for authorized discovery and `outcome:"rejected"` with status 401 and
`error:"unauthorized"` for a missing or wrong key. Records carry the request ID,
status, and outcome; they never carry aliases, headers, or payloads.

Logs exclude prompts, outputs, URLs, credentials, raw headers, and raw SDK errors.
Use non-sensitive route/model/endpoint labels: labels are intentionally logged.
Dependency logs are disabled in the CLI. In-process callers use
`with silence_dependency_logs():` so the previous logging level is restored on
exit. Docker rotates three 10 MB files. No metrics collector, tracing service,
spend store, or UI is included.

## Architecture and upgrades

`src/switchyard_gateway/domain.py`, `ports.py`, and `application.py` depend only on
standard-library and gateway-owned types. Concrete adapters live under `adapters/`;
`bootstrap.py` wires their lifetimes. The ports are `TierRouter`,
`HistoryCompressor`, `BackendTransport`, and `EventSink`.

Switchyard 0.2.0 calls model-client targets. Its adapter supplies capture clients
that record the selected request and return an internal synthetic completion.
The stage algorithm must invoke exactly one capture client; the synthetic result
never reaches clients or real backends. Message translation and request patches
stay inside the adapter. This is deliberately stage-specific, not a general runner
for algorithms that need intermediate inference calls.

Headroom's adapter translates options/results, disables Kompress, and runs its
synchronous work off the event loop. It recognizes zeroed failure metrics as
unknown rather than claiming successful compression. Application policy restores
protected content and rejects changes to message roles, tool IDs, or tool calls.

To upgrade, change exact dependency pins, regenerate the lockfile, adapt only the
affected adapter as needed, and run the contract/regression suite and image smoke
check. The architecture test rejects vendor imports in application/domain code.

## Verification

```bash
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m 'not live'
uv lock --check
uv run python scripts/smoke.py
docker compose config --quiet
docker build -t switchyard-gateway:check .
uv run python scripts/container_smoke.py
uv run python scripts/benchmark.py --iterations 30
```

### Test tiers

The offline suite is split by marker. No tier calls a real model.

```bash
uv run pytest -m unit          # pure logic through fake ports and properties
uv run pytest -m integration   # real pinned adapters, transport, and ASGI ingress
uv run pytest -m smoke         # real adapters end to end over fake local backends
uv run pytest -m 'not live'    # every tier above
```

`unit` uses Hypothesis properties for history protection, structural comparison,
round-robin distribution, routing-tier validation, retry bounds, and cooldown
parsing. `integration` exercises the pinned Switchyard and Headroom libraries.
`smoke` wires both real adapters through the ASGI app against a fake backend and
checks routing, escalation, structural savings, bypass, and log privacy.

Coverage and mutation checks run offline:

```bash
uv run pytest -m 'not live' --cov=src/switchyard_gateway --cov-branch --cov-report=term-missing
uv run mutmut run
uv run mutmut results
```

`[tool.mutmut]` targets the routing and compression policy in `application.py`
and `domain.py` and selects the unit tests, including the property suite. It
writes a disposable `mutants/` directory, which is untracked.

The container smoke test uses Linux host networking and disposable local fake
backends; it does not read your backend configuration.

The benchmark uses real routing/compression and a fake transport. It reports warm
preparation and compression p50/p95 measurements, excluding network/inference.
It is not a comparison against LiteLLM or a promise of end-to-end speedup.

Real backend checks cost tokens, including one capable-tier escalation. Explicitly
set `PROXY_URL`, `GATEWAY_API_KEY`, `LIVE_PAIR`, `LIVE_EFFICIENT_MODEL`, and
`LIVE_CAPABLE_MODEL`, then run `uv run pytest -m live`. The manual GitHub workflow
uses the `live` environment's URL/key secrets and route/model variables.

## Migrating from the LiteLLM deployment

This is a breaking configuration change. Create the new JSONC config and `.env`
from the examples; translate each old pair into two named model definitions and a
pair reference. Use bare backend IDs, without LiteLLM's `openai/` prefix. Translate
any required session headers into `forward_headers`. Move reasoning settings into
model defaults using the backend's native field names.

Stop the old deployment before binding its port. Start this branch with
`docker compose up -d --build --wait`. The new Compose file does not manage or
remove old containers; stop any orphaned old containers explicitly. Do not use
`down -v`: existing database volumes remain untouched and available for rollback.
Old keys, spend endpoints, guardrail body fields, admin UI, and
`x-litellm-model-name` are replaced by the gateway key, bypass header, JSON logs,
and `x-gateway-model`.

Main-branch publishing produces `ghcr.io/gyud-ai/switchyard-gateway` with the
project-version and commit-SHA tags. Set `GATEWAY_IMAGE` to a published tag and
pull it when deploying a release; the default local build works before publication.
Release versioning comes from `pyproject.toml`; tags/releases remain automated.

## License

Apache-2.0. Built on [NVIDIA Switchyard](https://github.com/NVIDIA-NeMo/Switchyard)
and [Headroom](https://github.com/headroomlabs-ai/headroom), both Apache-2.0.
