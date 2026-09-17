<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# app-lifespan. Application Lifespan Construction and Shutdown

The composition root assembles the concrete adapters — a pooled HTTP client, a
Headroom compressor with one execution thread, a JSON event sink, a Switchyard
router, an HTTPX transport, and the Gateway — then wraps them in a FastAPI app
whose lifespan emits one startup event and, at termination, releases the HTTP
client and joins the compression executor.

- **Entry point:** the `lifespan` async context manager defined inside
  `src/switchyard_gateway/bootstrap.py:build_app`
- **Trigger:** the operator runs the CLI (`switchyard_gateway.bootstrap:main`),
  which loads configuration, calls `build_app`, and passes the app to
  `uvicorn.run(..., workers=1)`.
- **Termination:** sink at `STDOUT` for the startup-event branch; the lifecycle
  returns to the initiator `OPERATOR` when `uvicorn.run` returns (seq 39). On the
  success path the HTTP client and compression executor are released in the
  lifespan `finally` block.
- **Response:**
  - **Success:** the ASGI server starts serving after the lifespan yields; on
    shutdown the pool and executor are closed and the process exits.
  - **Failure:** a configuration error is emitted as a sanitized `startup_failed`
    event and the process exits via `SystemExit(1)` before the server binds.
  - **Exception:** an exception from the startup `emit`, from `client.aclose()`,
    or from `compressor.close()` propagates out of the lifespan with no further
    cleanup (see Anomalies).

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `OPERATOR` | 1 | initiator | `main` | CLI entry: loads config, builds the app, starts one uvicorn worker. | `src/switchyard_gateway/bootstrap.py` |
| `UVICORN_SERVER` | 7 | intermediary | `uvicorn.run` | Single-process ASGI server and event loop; drives lifespan start/stop. | `src/switchyard_gateway/bootstrap.py` |
| `CONFIG` | 4 | intermediary | `load_config` | JSONC adapter resolving env references into validated `Settings`. | `src/switchyard_gateway/adapters/config.py` |
| `BUILD_APP` | 4 | intermediary | `build_app` | Composition root constructing every adapter and the Gateway. | `src/switchyard_gateway/bootstrap.py` |
| `GATEWAY` | 3 | intermediary | `Gateway` | Application core holding injected ports plus process-local routing state. | `src/switchyard_gateway/application.py` |
| `HTTP_CLIENT` | 4 | intermediary | `httpx.AsyncClient` | Pooled async client released by `aclose()` on shutdown. | `src/switchyard_gateway/bootstrap.py` |
| `COMPRESSOR` | 4 | intermediary | `HeadroomCompressor` | Headroom adapter owning one execution thread and a capacity semaphore. | `src/switchyard_gateway/adapters/headroom.py` |
| `COMPRESSION_EXECUTOR` | 7 | intermediary | `ThreadPoolExecutor` | The compression worker thread, joined by `shutdown(wait=True)`. | `src/switchyard_gateway/adapters/headroom.py` |
| `EVENTS` | 4 | intermediary | `JsonEvents` | JSON-lines sink writing sanitized gateway records to stdout. | `src/switchyard_gateway/adapters/logging.py` |
| `STDOUT` | 7 | sink | `sys.stdout` | Process stdout receiving the startup record. | `src/switchyard_gateway/adapters/logging.py` |
| `ROUTER` | 4 | intermediary | `SwitchyardRouter` | Switchyard stage-routing adapter; construction only stores policy. | `src/switchyard_gateway/adapters/switchyard.py` |
| `TRANSPORT` | 4 | intermediary | `HttpxTransport` | Backend transport wrapping the injected pooled client. | `src/switchyard_gateway/adapters/httpx.py` |
| `CREATE_APP` | 2 | intermediary | `create_app` | Ingress factory building the FastAPI app and recording the lifespan. | `src/switchyard_gateway/adapters/ingress.py` |
| `FASTAPI_APP` | 2 | intermediary | `FastAPI` | ASGI app that enters and resumes the lifespan context manager. | `src/switchyard_gateway/adapters/ingress.py` |
| `LIFESPAN` | 4 | intermediary | `lifespan` | Context manager emitting startup and releasing client/executor. | `src/switchyard_gateway/bootstrap.py` |

Group legend: 1=Client, 2=Ingress, 3=Application core, 4=Gateway adapter, 7=Process/OS.

## Sequence

1. (seq 1–2) `main` (`bootstrap.py:main`) calls `load_config(args.config)`
   (`adapters/config.py:load_config`) and receives a validated `Settings` carrying
   model/pair labels and `compression_workers`.
2. (seq 3) `main` calls `build_app(settings)` (`bootstrap.py:build_app`).
3. (seq 4–5) `build_app` constructs the shared `httpx.AsyncClient` with explicit
   timeouts, `follow_redirects=False`, and `trust_env=False` (`bootstrap.py:26-30`).
4. (seq 6–9) `build_app` constructs `HeadroomCompressor(settings.compression_workers)`
   (`bootstrap.py:31`); its `__init__` creates
   `ThreadPoolExecutor(max_workers=1, thread_name_prefix="compression")` and a
   `Semaphore(workers)` that bounds running-plus-queued work (`adapters/headroom.py:15-19`).
5. (seq 10–11) `build_app` constructs `JsonEvents()` bound to `sys.stdout`
   (`bootstrap.py:32`; `adapters/logging.py:15-16`).
6. (seq 12–15) Inside the `Gateway(...)` argument list, `build_app` constructs
   `SwitchyardRouter(settings.stage)` — which only stores the `StagePolicy`, making
   no SDK call (`adapters/switchyard.py:85-86`) — and `HttpxTransport(client)`,
   which only stores the client (`adapters/httpx.py:43-44`).
7. (seq 16–17) `build_app` constructs
   `Gateway(settings, router, compressor, transport, events)`
   (`bootstrap.py:33-35`; `application.py:68-87`). The Gateway stores the injected
   ports and starts with empty `_positions` and `_cooldowns` dictionaries.
8. (seq 18–21) `build_app` calls `create_app(gateway, lifespan)`
   (`bootstrap.py:48`), which builds
   `FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)`
   (`adapters/ingress.py:174-179`) and returns it.
9. (seq 22–23) `build_app` returns the app to `main`; `main` calls
   `uvicorn.run(app, host, port, workers=1, access_log=False, log_config=None)`
   (`bootstrap.py:71-78`).
10. (seq 24–25) Uvicorn drives ASGI lifespan startup and enters the `lifespan`
    context manager (`bootstrap.py:37-38`).
11. (seq 26–29) `lifespan` calls
    `events.emit({"event": "startup", "models": len(settings.models), "pairs": len(settings.pairs)})`
    (`bootstrap.py:39-41`). `JsonEvents.emit` prepends a UTC timestamp, serializes
    one compact JSON line, and flushes it to `STDOUT` (`adapters/logging.py:18-22`).
12. (seq 30) `lifespan` yields (`bootstrap.py:43`); startup completes and the
    server begins serving.
13. (seq 31) On shutdown the generator is resumed into the `finally` block
    (`bootstrap.py:44`).
14. (seq 32–33) `await client.aclose()` releases the pooled connections
    (`bootstrap.py:45`).
15. (seq 34–37) `await compressor.close()` (`bootstrap.py:46`) runs
    `HeadroomCompressor.close`, which awaits
    `asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)`:
    in-flight compression finishes and queued submissions are cancelled
    (`adapters/headroom.py:64-66`).
16. (seq 38–39) The lifespan exits cleanly; `uvicorn.run` returns to `main` and the
    process falls through to exit (`bootstrap.py:71-78`).

## Error paths

### Startup configuration failure

_Covers:_ seq 40, `CONFIG` → `OPERATOR` (outcome: error); seq 41, `OPERATOR` → `EVENTS` (outcome: error); seq 42, `EVENTS` → `STDOUT` (outcome: error); seq 43, `OPERATOR` → `UVICORN_SERVER` (outcome: error)

`load_config` wraps missing/empty environment references
(`adapters/config.py:176-186`) and any parse/validation failure
(`adapters/config.py:189-197`) in `GatewayError("invalid_configuration", 500)` or
`GatewayError("missing_configuration_environment", 500)`. `main` also raises
`GatewayError("litellm_must_not_be_installed", 500)` when the forbidden package is
importable (`bootstrap.py:62-63`). The handler at `bootstrap.py:65-67` emits
`{"event": "startup_failed", "error": error.code}` — only the sanitized code — and
raises `SystemExit(1)`, so `uvicorn.run` is never reached and no port is bound.
This is the only handled-error path in the lifecycle; the lifespan itself contains
no `except`. (The `--check` branch at `bootstrap.py:68-70` emits
`configuration_valid` and returns without starting the server; it is a separate
operator workflow, not an error.)

## Anomalies

### Startup emit sits outside the shutdown guard

_Covers:_ seq 44, `EVENTS` → `LIFESPAN` (outcome: anomaly)

The startup `events.emit(...)` call is placed before the `try:` at
`bootstrap.py:39-42`; the `finally` that closes the client and compressor only
covers `yield` (`bootstrap.py:43-46`). `JsonEvents.emit` performs a write and a
flush on stdout (`adapters/logging.py:18-22`), both of which can raise (broken
pipe, disk full, closed stream). If that happens the `try/finally` is never
entered, so `client.aclose()` and `compressor.close()` never run and the freshly
created executor is never shut down. The intended sequence is seq 26/29; the
anomalous return is seq 44.

### Sequential cleanup skips the compressor when the client close raises

_Covers:_ seq 45, `LIFESPAN` → `COMPRESSOR` (outcome: anomaly)

The `finally` body is two unguarded awaits in sequence
(`bootstrap.py:45-46`). If `await client.aclose()` raises, the second statement
`await compressor.close()` is skipped, leaving the one-thread executor
(`adapters/headroom.py:18`) unjoined. Each close is individually important and
neither is shielded; the omission is invisible in the JSON event stream because
no shutdown event is emitted at all (see below).

### Resources are constructed before the lifespan owns them

_Covers:_ seq 46, `UVICORN_SERVER` → `LIFESPAN` (outcome: anomaly)

`build_app` creates the `httpx.AsyncClient` and the `HeadroomCompressor` executor
at `bootstrap.py:26-35`, but cleanup exists only inside the lifespan context
manager that uvicorn later enters. The call is
`uvicorn.run(build_app(settings), ...)` (`bootstrap.py:71-74`), so construction
finishes before the server starts. If `uvicorn.run` fails to bind (or
`create_app`/`Gateway` raises before the return), the lifespan is never entered,
its `finally` never runs, and nothing shuts the executor down. Threads are created
lazily by `ThreadPoolExecutor`, so the leak is bounded in practice, but the
ownership handoff is not atomic: resources exist outside any started lifecycle.

### Orderly shutdown is unobservable in the event log

_Covers:_ seq 47, `LIFESPAN` → `EVENTS` (outcome: anomaly)

The lifecycle emits exactly one event, `startup`, at `bootstrap.py:39-41`, and
nothing on teardown: the `finally` only closes resources (`bootstrap.py:44-46`).
Other lifecycle events exist (`startup_failed`, `configuration_valid` at
`bootstrap.py:66,69`; per-request events emitted by `Gateway.finish`), but there is
no `shutdown`/`stopped` record, so an operator cannot distinguish a clean lifespan
exit from one that skipped cleanup. This is recorded as an expected-but-absent
branch; it may be intentional, but it is the reason the two anomalies above cannot
be observed from logs.

## Verification

- `src/switchyard_gateway/bootstrap.py:build_app` — confirmed the client,
  `HeadroomCompressor`, `JsonEvents`, `SwitchyardRouter`, `HttpxTransport`, and
  `Gateway` are constructed here and that `create_app(gateway, lifespan)` is the
  return value.
- `src/switchyard_gateway/bootstrap.py:lifespan` — confirmed `emit` is outside the
  `try`, `yield` is inside it, and the `finally` awaits `client.aclose()` then
  `compressor.close()`.
- `src/switchyard_gateway/bootstrap.py:main` — confirmed config load precedes
  `build_app`, that `uvicorn.run(..., workers=1)` starts the app, and that config
  failure emits `startup_failed` and raises `SystemExit(1)`.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.__init__` —
  confirmed the executor is `max_workers=1` and the semaphore is sized from
  `workers`.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.close` —
  confirmed `shutdown(wait=True, cancel_futures=True)` is offloaded with
  `asyncio.to_thread`.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` — confirmed one JSON
  line plus flush per record, timestamped, with no raw exception serialization.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — confirmed the lazy
  `lifespan` parameter is passed to `FastAPI(lifespan=lifespan)`.
- `src/switchyard_gateway/adapters/switchyard.py:SwitchyardRouter.__init__` and
  `src/switchyard_gateway/adapters/httpx.py:HttpxTransport.__init__` — confirmed
  construction performs no network or SDK work.
- `src/switchyard_gateway/application.py:Gateway.__init__` — confirmed injected
  ports are stored and `_positions`/`_cooldowns` start empty.
- `src/switchyard_gateway/adapters/config.py:load_config` — confirmed the two
  sanitized `GatewayError` codes raised on invalid/missing-env configuration.
- `tests/test_adapters.py:TestHeadroomContract` — pins compressor savings,
  fail-open `failed_unknown`, ML-disabled config, and `close()` in each `finally`.
- `tests/test_smoke.py:wired` — replicates the adapter wiring and closes
  client/compressor manually. Gap found: it calls `create_app(gateway)` with no
  lifespan, and no test imports `bootstrap.build_app` (`rg 'build_app|bootstrap'`
  finds only `pyproject.toml` and docs), so the real startup-event and shutdown
  code paths are not exercised offline.

## Serialization notes

### Group assignment for the composition root

- The legend lists "JSONC config" and "JSON events" under group 4 (Gateway
  adapter), so `CONFIG`, `EVENTS`, `BUILD_APP`, and `LIFESPAN` are group 4 even
  though `build_app`/`lifespan` are composition, not an adapter. `UVICORN_SERVER`,
  `COMPRESSION_EXECUTOR`, and `STDOUT` are group 7 (Process/OS); `GATEWAY` is
  group 3; `CREATE_APP`/`FASTAPI_APP` are group 2.

### Role assignments

- `OPERATOR` is `initiator` only under the initiator-wins convention: it starts the
  process and receives the final return (seq 39); its termination is derived from
  that incoming edge, not declared.
- `STDOUT` is `sink`: the startup-event branch (seq 27) terminates there.
- No `dead-end` node was declared. The anomalies are carried by `outcome:
  "anomaly"` links instead, because the premature terminations happen to absent
  interactions rather than to a participant that receives an unfinished flow.

### Seq ordering and kind mapping

- seq 1–23 follow construction order, including Python argument evaluation order:
  `SwitchyardRouter` (seq 12–13) and `HttpxTransport` (seq 14–15) are built while
  evaluating `Gateway(...)`, before `Gateway.__init__` at seq 16.
- seq 24–39 are the runtime startup/serve/shutdown order; seq 40–43 are the
  handled configuration-failure branch; seq 44–47 are anomaly annotations.
  Non-success links are numbered after the happy path because `seq` is only a
  total order and the error/anomaly subgraphs are extracted by filtering.
- Constructor invocations are `call` and their results `return`; ASGI lifespan
  signals, the startup emission, and the stdout write are `event`; the two
  expected-but-absent anomalies (seq 46, 47) are `note` because no runtime message
  is ever exchanged.
- `multigraph` is `true` because `LIFESPAN → COMPRESSOR`, `LIFESPAN → EVENTS`, and
  `EVENTS → LIFESPAN` each appear twice at different seq values (happy path plus
  anomaly). The schema permits this but a non-multigraph renderer would collapse
  the pairs.
- The vendor SDKs `switchyard.libsy` (group 5) and `headroom` (group 5) are
  intentionally absent: neither is called during startup. `SwitchyardRouter` only
  stores a `StagePolicy`, and `HeadroomCompressor` only creates an executor and a
  semaphore; the module-level `from headroom import ...` executes at process
  import, before this lifecycle begins.

## References

### Specs and decisions

- [Data Flow Graph schema](data-flow-graph.schema.json) — contract stamped as
  `urn:data-flow-graph:schema:3` (root `version: 3`).
- [README](../../README.md) §Architecture and upgrades — "`bootstrap.py` wires
  their lifetimes"; ports are `TierRouter`, `HistoryCompressor`,
  `BackendTransport`, `EventSink`.
- [README](../../README.md) §Configuration — "Cooldowns and round-robin positions
  are process-local; run one worker", matching the empty `_positions`/`_cooldowns`
  created at construction.
- [AGENTS.md](../../AGENTS.md) — "Bootstrap owns adapter construction and
  shutdown."

### Related lifecycles

- [health-check](health-check.md) — shares `UVICORN_SERVER` and `FASTAPI_APP`; a
  probe only succeeds after this lifespan has bound and served.
- [cli-startup](cli-startup.md) — the operator/CLI path that calls `build_app` and
  enters this same `lifespan`; it reuses different node IDs for the same code.
- [chat-stream](chat-stream.md) — per-request traffic that depends on the pooled
  client and compression executor this lifespan creates and releases.

### Code

- `src/switchyard_gateway/bootstrap.py:build_app` — adapter construction and app assembly.
- `src/switchyard_gateway/bootstrap.py:lifespan` — startup emit and shutdown release.
- `src/switchyard_gateway/bootstrap.py:main` — config load, failure handling, uvicorn startup.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor` — executor and close.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents` — startup record to stdout.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — FastAPI app with lifespan.
- `src/switchyard_gateway/adapters/config.py:load_config` — Settings provenance and failure codes.
- `src/switchyard_gateway/application.py:Gateway` — application core and in-memory state.
