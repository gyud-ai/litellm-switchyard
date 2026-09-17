<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# chat-stream. Stream upstream SSE to the client with alias rewriting, usage capture, and guaranteed cleanup

A Chat Completions client posts `stream: true`; the gateway validates and opens exactly one
upstream SSE response, rewrites each complete event's `model` to the requested alias while
capturing provider `usage`, forwards `[DONE]`, and always closes the upstream and emits one
terminal event even when the stream is interrupted or the client cancels.

- **Entry point:** `POST /v1/chat/completions` handled by `create_app.chat` in `src/switchyard_gateway/adapters/ingress.py:218`, delivered by `OwnedStream.__call__` (`ingress.py:164`) and the `sse_body` async generator (`ingress.py:109`)
- **Trigger:** An authenticated caller sends a Chat Completions payload with `"stream": true`
- **Termination:** Sink node `EVENTS` (terminal record) after `GATEWAY_FINISH`; the response itself returns to the `CLIENT` initiator. `uvicorn.run` (`bootstrap.py:98`) owns the process lifetime.
- **Response:**
  - **Success:** HTTP 200 `text/event-stream` with alias-rewritten frames, preserved tool deltas, captured `usage` in the final event record, and a terminating `data: [DONE]`
  - **Failure:** A sanitized JSON error body (`_error`, `ingress.py:101`) with `x-request-id` and a status of 400/401/404/413/502/503/504/500
  - **Exception:** A mid-stream upstream failure or client cancellation ends the SSE body early with no `[DONE]`; the shielded `finally` in `OwnedStream.__call__` still closes the upstream and emits one terminal event

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `CLIENT` | 1 | initiator | — | External OpenAI-compatible HTTP caller that requests `stream=true` and consumes SSE frames. | external HTTP caller |
| `UVICORN` | 7 | intermediary | `uvicorn.run` | Single-worker ASGI server and event loop that accepts the socket and drives the FastAPI app. | `src/switchyard_gateway/bootstrap.py:98` |
| `CHAT_ROUTE` | 2 | intermediary | `create_app.chat` | FastAPI route handler for POST /v1/chat/completions; owns request validation, handoff, and error responses. | `src/switchyard_gateway/adapters/ingress.py:218` |
| `AUTHORIZER` | 2 | intermediary | `create_app.authorize` | Constant-time Bearer API-key check; raises `unauthorized` on mismatch. | `src/switchyard_gateway/adapters/ingress.py:187` |
| `VALIDATOR` | 2 | intermediary | `_validate` | Validates model, messages, roles, text content, tool calls, and tool results into a `Payload`. | `src/switchyard_gateway/adapters/ingress.py:41` |
| `GATEWAY_OPEN` | 3 | intermediary | `Gateway.open` | Application orchestration: routes the alias, protects history, compresses, selects a replica, and opens one upstream exchange. | `src/switchyard_gateway/application.py:116` |
| `ELIGIBLE_HISTORY` | 3 | intermediary | `eligible_indices` | Selects compressible older history while protecting instructions, cached rows, and complete live tool exchanges. | `src/switchyard_gateway/application.py:27` |
| `ROUTER` | 4 | intermediary | `SwitchyardRouter.route` | Tier router adapter; normalizes messages, invokes the stage algorithm, and restores original messages. | `src/switchyard_gateway/adapters/switchyard.py:88` |
| `SWITCHYARD_SDK` | 5 | intermediary | `algorithms.stage_router` | Pinned `switchyard.libsy` stage router invoked with capture clients; its synthetic completion never reaches a backend or client. | `src/switchyard_gateway/adapters/switchyard.py:99` |
| `COMPRESSOR` | 4 | intermediary | `HeadroomCompressor.compress` | Compression adapter that serializes access to Headroom's shared pipeline through a bounded slot semaphore. | `src/switchyard_gateway/adapters/headroom.py:50` |
| `THREAD_POOL` | 7 | intermediary | `ThreadPoolExecutor` | Single compression execution thread; cancellation does not stop a running thread and the slot is released only when it exits. | `src/switchyard_gateway/adapters/headroom.py:18` |
| `HEADROOM_SDK` | 5 | intermediary | `compress` | Pinned `headroom-ai` synchronous compressor; ML compression disabled and failures returned as `failed_unknown`. | `src/switchyard_gateway/adapters/headroom.py:25` |
| `TRANSPORT` | 4 | intermediary | `HttpxTransport.send` | HTTPX-backed transport adapter; opens one streamed upstream request with normalized failures and no hidden retries. | `src/switchyard_gateway/adapters/httpx.py:46` |
| `BACKEND` | 6 | intermediary | — | External OpenAI-compatible upstream that produces the SSE completion. | external upstream |
| `HTTPX_RESPONSE` | 4 | intermediary | `HttpxResponse` | Open upstream response adapter exposing status, headers, chunk iteration, and idempotent close. | `src/switchyard_gateway/adapters/httpx.py:11` |
| `GATEWAY_BODY` | 3 | intermediary | `Gateway.body` | Reads upstream bytes and records `first_body_byte_ms` on the first non-empty chunk. | `src/switchyard_gateway/application.py:242` |
| `SSE_BODY` | 2 | intermediary | `sse_body` | Incremental SSE rewriter: buffers complete events, rewrites `model` to the alias, captures `usage`, and emits `[DONE]`. | `src/switchyard_gateway/adapters/ingress.py:109` |
| `OWNED_STREAM` | 2 | intermediary | `OwnedStream.__call__` | ASGI `StreamingResponse` subclass whose `finally` block always finishes the exchange under a shielded cancel scope. | `src/switchyard_gateway/adapters/ingress.py:164` |
| `GATEWAY_FINISH` | 3 | intermediary | `Gateway.finish` | Closes the upstream response and emits exactly one terminal request event with the recorded outcome. | `src/switchyard_gateway/application.py:251` |
| `EVENTS` | 4 | sink | `JsonEvents.emit` | Sanitized JSON event sink writing one record per line to stdout. | `src/switchyard_gateway/adapters/logging.py:18` |

## Sequence

1. (seq 1–2) An authenticated client posts to `/v1/chat/completions` with `"stream": true`; the single Uvicorn worker dispatches the ASGI scope to `create_app.chat` (`ingress.py:218`).
2. (seq 3–4) `chat` calls `authorize` (`ingress.py:187`), which compares `Authorization` to `Bearer {gateway.settings.api_key}` with `hmac.compare_digest`; the check returns cleanly.
3. (seq 5–6) `chat` accumulates `request.stream()` into a `bytearray`, enforcing `gateway.settings.max_request_bytes` (`ingress.py:228-232`), then `json.loads(..., parse_constant=_reject_constant)` and `_validate` (`ingress.py:41`) return a validated `Payload`; `opened` becomes `True`.
4. (seq 7–11) `chat` calls `Gateway.open` (`application.py:116`). Inside `_open` (`application.py:132`) the pair alias resolves and `SwitchyardRouter.route` (`switchyard.py:88`) normalizes messages and runs `algorithms.stage_router` with `_CaptureClient`s (`switchyard.py:63`). Exactly one capture client records one request; the synthetic completion is discarded and the original messages are restored, yielding a `RoutingResult`.
5. (seq 12–13) `eligible_indices` (`application.py:27`) returns the compressible older-history indices, retaining system/developer rows, cache-marked rows, the latest user/assistant turn, and connected live tool exchanges.
6. (seq 14–19) `HeadroomCompressor.compress` (`headroom.py:50`) submits `_compress` to the single `ThreadPoolExecutor` (`headroom.py:18`), which calls `headroom.compress` with `kompress_model="disabled"` and returns a `CompressionResult` (`savings`, `no_savings`, or `failed_unknown`). The application rejects any result whose message structure changed (`_same_structure`, `application.py:58`).
7. (seq 20–24) `Gateway._select` round-robins an endpoint and `HttpxTransport.send` (`httpx.py:46`) opens `POST {base_url}/chat/completions` with `httpx` `stream=True`, returning an `HttpxResponse`. `_open` returns an `Exchange` carrying the open response, the event record, and start timings.
8. (seq 25–26) Since `payload["stream"]` is truthy and the upstream `content-type` contains `text/event-stream` (`ingress.py:247-251`), `chat` sets `handed_off = True` and returns `OwnedStream(gateway, exchange, payload["model"], response_headers)`; Uvicorn then invokes `OwnedStream.__call__` (`ingress.py:164`), which delegates to `StreamingResponse.__call__`.
9. (seq 27–33) Starlette's streaming driver iterates `sse_body` (`ingress.py:109`), which pulls chunks through `Gateway.body` (`application.py:242`) and `HttpxResponse.chunks` (`httpx.py:27`). `Gateway.body` records `first_body_byte_ms` on the first non-empty chunk.
10. (seq 34–35) `sse_body` splits complete events on the `\r?\n\r?\n` boundary, rewrites any top-level `model` to the requested alias, captures recognized integer `usage` fields into `exchange.event["usage"]`, preserves tool-call deltas, and yields each rewritten frame to the client. On `data: [DONE]` it sets `stream_outcome = "completed"` and yields the terminator (`ingress.py:122-126`).
11. (seq 36–37) The generator returns after `[DONE]`; the ASGI response completes and the client's SSE body is closed.
12. (seq 38–43) `OwnedStream.__call__`'s `finally` (`ingress.py:167-171`) pops `stream_outcome` and calls `Gateway.finish` (`application.py:251`) inside `anyio.CancelScope(shield=True)`. `finish` closes the upstream response, marks the exchange finished (idempotent), updates `outcome`/`total_ms`, and emits exactly one terminal event through `JsonEvents.emit` (`logging.py:18`).

## Error paths

### Unauthorized

_Covers:_ seq 50, 51, 52, 53

`authorize` (`ingress.py:187`) raises `GatewayError("unauthorized", 401)` when the Bearer header does not match. `chat`'s `except GatewayError` builds `_error` (`ingress.py:101`), a JSONResponse with the sanitized code and `x-request-id`. Because the rejection happens before `opened = True` and `exchange` stays `None`, `chat` emits a `{"event": "request", "outcome": "rejected"}` record (`ingress.py:272-281`).

### Invalid or oversized request

_Covers:_ seq 54, 55, 56, 57

`_validate` (`ingress.py:41`) raises 400-coded `GatewayError`s for a missing/non-string `model`, non-boolean `stream`, empty or malformed `messages`, unsupported content, and invalid tool calls/results. The body loop raises `request_too_large` (413) and the `json.loads` wrapper raises `invalid_json` (400). Since `opened` is still `False`, a rejected request event is emitted and `_error` returns the sanitized body.

### Unknown model

_Covers:_ seq 58, 59, 60, 61

When the alias is neither a pair nor a configured model, `_open` raises `GatewayError("unknown_model", 404)` (`application.py:152`). `Gateway.open`'s `except BaseException` records `outcome="failed"`, `status=404`, and the sanitized code, emits the event (`application.py:122-130`), and re-raises. `chat` returns the 404 body without emitting a second event because `opened` is already `True`.

### Upstream connect, timeout, or failover exhaustion

_Covers:_ seq 62, 63, 64, 65, 66

`HttpxTransport.send` (`httpx.py:46`) translates `httpx.ConnectError`/`ConnectTimeout` into `ConnectFailure`, other timeouts into `upstream_timeout` (504), and remaining transport errors into `upstream_transport_failed`. In `_open` (`application.py:186-240`), a `ConnectFailure` or retryable 429/502/503/504 cools the endpoint down and permits exactly one attempt on a different eligible replica; a second failure or no eligible replica raises `no_available_replica` (503). The selected tier never changes. The client receives the sanitized status.

### Upstream rejected or wrong content type before streaming

_Covers:_ seq 67, 68, 69, 70

After `open` returns, `chat` checks `exchange.response.status >= 400` and raises `upstream_rejected_request` with that status, and when `stream` is requested but the upstream `content-type` lacks `text/event-stream` it raises `invalid_upstream_stream` (`ingress.py:244-249`). Upstream bodies are never forwarded. `chat`'s `finally` then calls `finish(exchange, "failed")` because `handed_off` is `False`, closing the upstream and emitting the terminal event.

### Unexpected server failure

_Covers:_ seq 71, 72, 73

Any non-`GatewayError` exception is caught by `chat`'s `except Exception` (`ingress.py:286-299`), which records `status=500, error="request_failed"` on the exchange (or emits a failed request event when no exchange exists) and returns a sanitized 500 body. The exception text is never exposed.

## Anomalies

### Mid-stream failure is swallowed; the client stream ends without `[DONE]`

_Covers:_ seq 80, 81, 82

`sse_body` wraps its whole loop in `except Exception` and on any failure sets `exchange.event["stream_outcome"] = "interrupted"` and `return` (`ingress.py:146-149`). The upstream is still closed by `OwnedStream.__call__`'s shielded `finally`, and the terminal event records `interrupted`, but the HTTP status is already 200 and the client receives neither an error frame nor `data: [DONE]`: it observes a clean EOF that is indistinguishable from a short response except by the missing terminator. This path is exercised by `tests/test_ingress.py:test_malformed_or_incomplete_stream_is_interrupted` and `tests/test_ingress.py:test_midstream_read_failure_never_retries`, both of which assert no `[DONE]` and `outcome == "interrupted"` while the upstream is closed and never retried. The swallow also covers `upstream_event_too_large`, `upstream_stream_error`, and the `upstream_stream_incomplete` raised when upstream EOF arrives without `[DONE]` (`ingress.py:139-142`), so a backend that legitimately ends at EOF is misclassified as interrupted.

### Client cancellation is recorded but never reaches the client

_Covers:_ seq 85, 86, 87, 88

When the client disconnects before or during streaming, `sse_body` catches `asyncio.CancelledError`, sets `stream_outcome = "cancelled"`, and re-raises (`ingress.py:143-145`). `OwnedStream.__call__`'s `finally` then finishes the exchange with `"cancelled"` under `anyio.CancelScope(shield=True)`, so the upstream is released and one `cancelled` event is emitted; `chat`'s own `finally` skips `finish` because `handed_off` is `True`, preventing a double emit. `tests/test_ingress.py:test_cancellation_before_stream_iteration_releases_upstream` pins that the response is closed and the outcome is `cancelled`. This is the client's expected premature termination, but it is worth noting that the fallback `pop("stream_outcome", "cancelled")` also labels any abnormal exit that never recorded an outcome as a cancellation.

### Replica cooldown and round-robin state dies on restart

_Covers:_ seq 90

`Gateway.__init__` keeps `_positions` and `_cooldowns` as plain in-memory dicts (`application.py:86-87`). They are process-local by design and explicitly single-worker (README, Configuration), so a Uvicorn restart recreates the `Gateway` and forgets every cooldown and rotation position. A backend that was cooled down for an outage is immediately eligible again after restart, and round-robin restarts at the first endpoint; the state is only ever rebuilt by observation. `tests/test_application.py:test_fresh_cooldown_table_keeps_replicas_eligible` documents that a fresh table keeps all replicas eligible.

## Verification

- `src/switchyard_gateway/adapters/ingress.py:create_app.chat` — traced authorization, body cap, JSON/validation, `gateway.open`, status/content-type gate, `handed_off`, `OwnedStream` construction, `except GatewayError`/`CancelledError`/`Exception`, and the `finally` finish guard.
- `src/switchyard_gateway/adapters/ingress.py:sse_body` — traced event buffering, `_EVENT_END` splitting, alias rewrite, `_usage` capture, `[DONE]` handling, both exception arms, and the `upstream_event_too_large`/`upstream_stream_incomplete` raises.
- `src/switchyard_gateway/adapters/ingress.py:OwnedStream.__call__` — confirmed the shielded `finally` `gateway.finish` and the `stream_outcome` pop default.
- `src/switchyard_gateway/adapters/ingress.py:_validate` and `_usage` — confirmed every 400 code and the usage allowlist.
- `src/switchyard_gateway/application.py:Gateway.open` / `_open` / `_select` / `_cooldown` — confirmed routing, `eligible_indices`, compression fail-open, round-robin, bounded failover, cooldown, and the `BaseException` event emit/re-raise.
- `src/switchyard_gateway/application.py:Gateway.body` / `finish` — confirmed first-body-byte timing, idempotent `finished` guard, close, and exactly-one terminal event.
- `src/switchyard_gateway/adapters/switchyard.py:SwitchyardRouter.route` — confirmed capture-client stage routing and that exactly one request is captured.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.compress` / `_compress` — confirmed executor isolation, fail-open `failed_unknown`, and slot release on thread exit.
- `src/switchyard_gateway/adapters/httpx.py:HttpxTransport.send` / `HttpxResponse.chunks` / `close` — confirmed streamed POST, header auth, and normalized transport failures.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` — confirmed one sanitized JSON line per terminal event.
- `src/switchyard_gateway/bootstrap.py:build_app` / `main` / `uvicorn.run` — confirmed single-worker composition and process ownership.
- `tests/test_ingress.py:TestStreams` — pins fragmented SSE rewriting, alias/usage capture, the interrupted-without-`[DONE]` path, no midstream retry, and cancellation releasing the upstream.
- `tests/test_application.py:TestReplicaPolicyContracts` and `tests/test_adapters.py:TestCompressionCancellation` — pin cooldown/round-robin state and compression-slot behavior.
- `docs/dataflow/data-flow-graph.schema.json` — contract revision 3 validated by `jsonschema` and `check_graphs.py`.

## Serialization notes

### Roles

- `CLIENT` is the only `initiator` (initiator-wins): it starts the cycle and receives the streamed return, so its termination is derived from incoming return edges rather than declared.
- `EVENTS` is the only `sink`, the terminal side effect with no outgoing edges; `BACKEND` is an `intermediary` because it returns a response rather than terminating.

### Seq ordering

- Happy-path steps run 1–43 in lifecycle order; handled-error branches occupy 50–73 and anomalies 80–90. Gaps keep the two subgraphs separable with a single total order and no duplicate seq values.

### Kind mapping

- Request/response pairs use `call`/`return`; SSE fan-out and terminal log emission use `event`; the restart-state observation uses `note`.

### Modeling choices

- SSE frames are modeled as `SSE_BODY → CLIENT` events rather than repeating the `SSE_BODY → OWNED_STREAM → UVICORN → CLIENT` byte relay for every frame; the relay is stated once in prose.
- All pre-stream handled errors originate at `CHAT_ROUTE` because that is where `_error` builds the JSONResponse; the raise site is captured by the preceding `outcome: error` return into `CHAT_ROUTE` (for example `GATEWAY_OPEN → CHAT_ROUTE` at seq 58).
- The restart-state anomaly is a `note` from `UVICORN` to `GATEWAY_OPEN` because the process lifetime, not the request, discards the state.

## References

### Specs and decisions

- [README.md](../../README.md) §Configuration — cooldowns, round-robin, and single-worker constraints on replica state.
- [README.md](../../README.md) §Client interface — streaming, `[DONE]` behavior, and header contract.
- [README.md](../../README.md) §Observability — completed/interrupted/cancelled stream outcomes.
- [AGENTS.md](../../AGENTS.md) §Contracts — one upstream call, guarded cleanup, and sanitized logs.

### Related lifecycles

- [chat-completion](chat-completion.md) — the non-streaming sibling: same
  ingress, routing, compression, replica policy, and terminal cleanup, but it
  buffers the body and returns one JSON response.
- [model-discovery](model-discovery.md) and [health-check](health-check.md) —
  other routes on the same `create_app` app.
- [cli-startup](cli-startup.md) and [app-lifespan](app-lifespan.md) — the
  composition and resource lifetime that the streaming exchange depends on.

### Code

- `src/switchyard_gateway/adapters/ingress.py:create_app.chat` — request validation, handoff, and pre-stream error surfacing.
- `src/switchyard_gateway/adapters/ingress.py:sse_body` — SSE rewriting, usage capture, and the swallowed mid-stream failure.
- `src/switchyard_gateway/adapters/ingress.py:OwnedStream.__call__` — shielded terminal cleanup.
- `src/switchyard_gateway/application.py:Gateway` — routing, compression, replica, body, and finish orchestration.
- `src/switchyard_gateway/adapters/httpx.py:HttpxResponse` — streamed upstream bytes and close.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` — terminal event serialization.
