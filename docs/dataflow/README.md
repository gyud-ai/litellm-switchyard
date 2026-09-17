# Data-flow graphs

Node-link graphs of every inbound boundary in this gateway, one lifecycle per
pair: `<slug>.json` (graph) + `<slug>.md` (narration). Both carry contract
revision 3 (`urn:data-flow-graph:schema:3`); the schema and markdown template
sit beside them.

Every view — sequence diagram, data-flow diagram, initiation/termination points,
success/error/anomaly paths — is a filter over one `.json`; no view joins files.
Ordering is the single `seq` value in each graph.

Validate mechanically:

```bash
python3 <trace-data-flow>/scripts/check_graphs.py docs/dataflow
```

## Group legend

| Group | Meaning |
| --- | --- |
| 1 | Client — external HTTP caller or operator |
| 2 | Ingress — FastAPI route, ASGI request/response, request validation |
| 3 | Application core — `Gateway` orchestration, domain types, ports |
| 4 | Gateway adapter — Switchyard/Headroom/HTTPX/JSONC config/JSON events |
| 5 | Vendor SDK — `switchyard.libsy`, `headroom` |
| 6 | External backend — OpenAI-compatible upstream |
| 7 | Process/OS — config file, stdout, HTTP server, thread pool, event loop |

Group numbers are a graph-local convention; bootstrap nodes (composition root)
are placed in group 3, the closest fit in this legend.

## Outcome and role legend

- `outcome`: `success` (happy path), `error` (handled failure surfaced to the
  caller), `anomaly` (verified-absent, swallowed, or prematurely terminating
  path — every anomaly also has a prose subsection in its pair).
- `role`: `initiator` (the client starts and receives the return),
  `intermediary` (default), `sink` (legitimate server-side termination),
  `dead-end` (termination without the intended response).

## Entry-point inventory

| Lifecycle | Entry point | Location | Graph | Nodes / links |
| --- | --- | --- | --- | --- |
| Health check | `GET /health/liveliness`, `GET /health/readiness` | [ingress.py:203](../../src/switchyard_gateway/adapters/ingress.py) `create_app.health` | [health-check.json](health-check.json) · [md](health-check.md) | 5 / 7 |
| Model discovery | `GET /v1/models` | [ingress.py:207](../../src/switchyard_gateway/adapters/ingress.py) `create_app.models` | [model-discovery.json](model-discovery.json) · [md](model-discovery.md) | 12 / 24 |
| Chat completion (JSON) | `POST /v1/chat/completions` | [ingress.py:227](../../src/switchyard_gateway/adapters/ingress.py) `create_app.chat` | [chat-completion.json](chat-completion.json) · [md](chat-completion.md) | 18 / 78 |
| Chat completion (SSE) | `POST /v1/chat/completions` with `stream: true` | [ingress.py:227](../../src/switchyard_gateway/adapters/ingress.py) `create_app.chat` → `sse_body` / `OwnedStream` | [chat-stream.json](chat-stream.json) · [md](chat-stream.md) | 20 / 76 |
| Application lifespan | FastAPI lifespan enter/exit | [bootstrap.py:29](../../src/switchyard_gateway/bootstrap.py) `build_app.lifespan` | [app-lifespan.json](app-lifespan.json) · [md](app-lifespan.md) | 15 / 52 |
| CLI startup | `switchyard-gateway [--config] [--check]` | [bootstrap.py:73](../../src/switchyard_gateway/bootstrap.py) `main` | [cli-startup.json](cli-startup.json) · [md](cli-startup.md) | 23 / 59 |

Out of scope: `scripts/smoke.py`, `scripts/container_smoke.py`, and
`scripts/benchmark.py` are operator/test tools, not product boundaries.

## Cross-lifecycle anomaly digest

Findings that recur across lifecycles are grouped; the pair prose is
authoritative and cites the code.

### Replica state dies on restart

- [chat-completion](chat-completion.md) and [chat-stream](chat-stream.md) —
  `application.py:Gateway.__init__` keeps `_positions`/`_cooldowns` in process
  memory, so a restart forgets round-robin position and cools nothing down.

### Failures swallowed or hidden from the caller

- [chat-stream](chat-stream.md) — a mid-stream upstream failure ends the SSE
  body with one sanitized `upstream_stream_interrupted` frame instead of a clean
  EOF, but the cause is collapsed into that single client-visible code.
  Cancellation is recorded but never reaches the client, and any outcome-less
  abnormal exit now defaults to `interrupted` rather than `cancelled`.
- [chat-completion](chat-completion.md) — `application.py:Gateway.finish`
  closes the upstream before emitting; if the close raises, the event is logged
  `completed` and the exception replaces the already-built JSON response.
- [chat-completion](chat-completion.md) — the non-streaming branch has no
  upstream `content-type` guard (the streaming branch does), so a wrong-type 200
  surfaces as a generic `invalid_upstream_response` 502.

### Probes and side effects absent where expected

- [health-check](health-check.md) — `/health/readiness` is the same
  unconditional handler as `/health/liveliness` and never consults `Settings`,
  the compressor, or the transport, so readiness cannot report a degraded state.
- [model-discovery](model-discovery.md) — `create_app.models` never calls
  `gateway.events.emit` on either the 200 or 401 path, so discovery and its
  rejections are invisible in the JSON event stream.
