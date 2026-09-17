<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# cli-startup. CLI Startup and Worker Launch

The `switchyard-gateway` console script parses its arguments under a `SystemExit`
guard so a malformed invocation still emits a sanitized JSON `startup_failed`
record, refuses to run in a LiteLLM-contaminated interpreter, loads and validates
a JSONC configuration into frozen `Settings`, and either exits after `--check`
validation or hands an app to a single Uvicorn worker whose lifespan constructs
and owns every adapter.

- **Entry point:** `switchyard_gateway.bootstrap:main`, exposed as the
  `switchyard-gateway` console script in `pyproject.toml`
- **Trigger:** an operator runs the console script from a shell, a Compose
  service, or a container entrypoint
- **Termination:** `STDOUT` (sink) for the `configuration_valid`, `startup`,
  `shutdown`, and `startup_failed` records; `UVICORN` (sink) for the running
  worker. `STDERR` (dead-end) still receives the argparse usage text for a
  malformed invocation, now alongside the JSON record.
- **Response:**
  - **Success:** `--check` prints `{"event":"configuration_valid"}` and exits 0;
    the server path prints `{"event":"startup",...}`, serves on `host:port`, and
    prints exactly one `{"event":"shutdown"}` on orderly teardown
  - **Failure:** `{"event":"startup_failed","error":"<sanitized_code>"}` on
    stdout followed by exit code 1 for configuration failures; argument errors
    emit `{"event":"startup_failed","error":"invalid_arguments"}` and preserve
    argparse's exit code 2
  - **Exception:** an unhandled Uvicorn start failure aborts with a traceback,
    but no adapter resource exists until the lifespan is entered, so nothing
    leaks; a lifecycle emit failure inside the lifespan releases every adapter
    before propagating

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `OPERATOR` | 1 | initiator | `switchyard-gateway` | Operator invoking the console script with --config and optionally --check. | `pyproject.toml` |
| `MAIN` | 3 | intermediary | `main` | Composition-root CLI entry: parses arguments under a SystemExit guard, guards LiteLLM, loads config, starts one Uvicorn worker. | `src/switchyard_gateway/bootstrap.py` |
| `ARG_PARSER` | 3 | intermediary | `argparse.ArgumentParser` | --config (Path, default config.jsonc) and --check argument parsing; usage errors exit 2 on stderr. | `src/switchyard_gateway/bootstrap.py` |
| `LITELLM_GUARD` | 3 | intermediary | `importlib.util.find_spec` | Preflight check that refuses an environment where litellm is importable. | `src/switchyard_gateway/bootstrap.py` |
| `BUILD_APP` | 3 | intermediary | `build_app` | Composes the event sink and the app whose lifespan constructs and owns every adapter. | `src/switchyard_gateway/bootstrap.py` |
| `GATEWAY` | 3 | intermediary | `Gateway` | Application orchestration core constructed by the lifespan and installed on `app.state`. | `src/switchyard_gateway/application.py` |
| `LIFESPAN` | 3 | intermediary | `lifespan` | Async context manager constructing adapters on an `AsyncExitStack`, emitting startup and one shutdown record. | `src/switchyard_gateway/bootstrap.py` |
| `INGRESS_APP` | 2 | intermediary | `create_app` | FastAPI ingress factory storing the lifespan and resolving the Gateway from `app.state`. | `src/switchyard_gateway/adapters/ingress.py` |
| `LOAD_CONFIG` | 4 | intermediary | `load_config` | JSONC parse, environment-reference resolution, and pydantic validation into Settings. | `src/switchyard_gateway/adapters/config.py` |
| `JSON_EVENTS` | 4 | intermediary | `JsonEvents` | One-JSON-record-per-line stdout event sink with UTC timestamps. | `src/switchyard_gateway/adapters/logging.py` |
| `SILENCE_LOGS` | 4 | intermediary | `silence_dependency_logs` | Scoped dependency-log suppression: disables logging at `CRITICAL` and restores the saved level when the returned silencer is released. | `src/switchyard_gateway/adapters/logging.py` |
| `HEADROOM` | 4 | intermediary | `HeadroomCompressor` | Compression adapter constructed inside the lifespan; registers its close on the lifespan's `AsyncExitStack`. | `src/switchyard_gateway/adapters/headroom.py` |
| `SWITCHYARD` | 4 | intermediary | `SwitchyardRouter` | Tier-routing adapter over the pinned Switchyard SDK. | `src/switchyard_gateway/adapters/switchyard.py` |
| `HTTPX_TRANSPORT` | 4 | intermediary | `HttpxTransport` | Single-attempt backend transport adapter over the pooled HTTPX client. | `src/switchyard_gateway/adapters/httpx.py` |
| `HTTPX_CLIENT` | 4 | intermediary | `httpx.AsyncClient` | Pooled HTTP client constructed inside the lifespan and owned until its `AsyncExitStack` releases it. | `src/switchyard_gateway/bootstrap.py` |
| `SWITCHYARD_SDK` | 5 | intermediary | `switchyard.libsy.algorithms` | Pinned vendor tier-routing SDK imported by the Switchyard adapter. | `src/switchyard_gateway/adapters/switchyard.py` |
| `HEADROOM_SDK` | 5 | intermediary | `headroom.compress` | Pinned vendor structural-compression SDK imported by the Headroom adapter. | `src/switchyard_gateway/adapters/headroom.py` |
| `CONFIG_FILE` | 7 | intermediary | `config.jsonc` | JSONC configuration document read from disk. | `config.example.jsonc` |
| `ENV` | 7 | intermediary | `os.environ` | Process environment holding telemetry switches and JSONC-resolved secrets/backend URLs. | `src/switchyard_gateway/bootstrap.py` |
| `STDOUT` | 7 | sink | `sys.stdout` | JSON event stream consumed by the operator and container log collector. | `src/switchyard_gateway/adapters/logging.py` |
| `STDERR` | 7 | dead-end | `sys.stderr` | argparse usage and error text for malformed invocations; a structured startup_failed record is emitted on stdout alongside it. | `src/switchyard_gateway/bootstrap.py` |
| `UVICORN` | 7 | sink | `uvicorn.run` | Single-worker ASGI server that terminates the startup path and owns the event loop. | `src/switchyard_gateway/bootstrap.py` |
| `THREAD_POOL` | 7 | intermediary | `ThreadPoolExecutor` | Compression execution thread owned by HeadroomCompressor, created when the lifespan constructs it. | `src/switchyard_gateway/adapters/headroom.py` |

## Sequence

1. (seq 1) `OPERATOR` invokes the `switchyard-gateway` console script
   (`pyproject.toml` `[project.scripts]` maps it to
   `switchyard_gateway.bootstrap:main`).
2. (seq 2-3) `MAIN` builds an `argparse.ArgumentParser` with `--config`
   (`type=Path`, default `Path("config.jsonc")`) and `--check`
   (`action="store_true"`) and calls `parse_args()` under a `SystemExit` guard
   (`bootstrap.py:75-83`). It receives back a `Namespace` with the parsed `Path`
   and boolean.
3. (seq 4) `MAIN` calls `silence_dependency_logs()` (`bootstrap.py:84`;
   `adapters/logging.py:silence_dependency_logs`). The call captures the
   current `logging.root.manager.disable` value, runs
   `logging.disable(logging.CRITICAL)`, and returns a `DependencyLogSilencer`
   carrying the captured level. `MAIN` discards the handle, so dependency logs
   stay silenced for the whole CLI process; in-process callers that use
   `with silence_dependency_logs():` restore the captured level on exit.
4. (seq 5) `MAIN` writes three telemetry switches into the process environment:
   `HEADROOM_BEACON=off`, `DO_NOT_TRACK=1`, `HEADROOM_TELEMETRY=off`
   (`bootstrap.py:85-87`).
5. (seq 6-7) Inside a `try`, `MAIN` calls `importlib.util.find_spec("litellm")`
   and requires it to return `None`; a non-`None` spec raises
   `GatewayError("litellm_must_not_be_installed", 500)`
   (`bootstrap.py:89-90`).
6. (seq 8-13) `MAIN` calls `load_config(args.config)`
   (`adapters/config.py:load_config`). `LOAD_CONFIG` reads the JSONC text via
   `Path.read_text()` (`CONFIG_FILE`), resolves complete-value `{"env": NAME}`
   references through `_resolve` against `os.environ` (`ENV`), rejects a missing
   or empty variable with `GatewayError("missing_configuration_environment", 500)`,
   and validates the document with `Config.model_validate(...).settings()`
   (`adapters/config.py:189-197`). It returns a frozen `Settings`.
7. (seq 14-16) **Alternate terminal, only when `args.check` is set.** `MAIN`
   emits `{"event":"configuration_valid"}` through a fresh `JsonEvents`, which
   writes and flushes one JSON line to `STDOUT`, then returns without starting a
   server (`bootstrap.py:95-97`).
8. (seq 17-20) On the server path, `MAIN` calls `build_app(settings)`
   (`bootstrap.py:99`). `BUILD_APP` creates the `JsonEvents` sink, calls
   `create_app(lifespan=lifespan)` (`bootstrap.py:70`), and returns the FastAPI
   app. No client, executor, router, transport, or Gateway exists yet: the
   lifespan it records will construct them (seq 23-34).
9. (seq 21-40) `MAIN` calls `uvicorn.run(app, host=..., port=..., workers=1,
   access_log=False, log_config=None)` (`bootstrap.py:98-105`). Uvicorn enters
   ASGI lifespan startup, which invokes `LIFESPAN`; `LIFESPAN` constructs the
   pooled `httpx.AsyncClient`, the `HeadroomCompressor` (creating the
   single-worker `ThreadPoolExecutor`), the `SwitchyardRouter`, the
   `HttpxTransport`, and the `Gateway`, installing the gateway on `app.state`
   (seq 23-35). It then emits
   `{"event":"startup","models":N,"pairs":M}` through `JSON_EVENTS` to `STDOUT`
   (`bootstrap.py:56-62`), and startup completes (seq 36-40).
10. (seq 41-45) On shutdown the lifespan resumes: the `AsyncExitStack` releases
    the compressor and then the client (seq 41), emits exactly one
    `{"event":"shutdown"}` record after release (seq 42-44), and the context
    exits (seq 45).
11. (seq 46) `uvicorn.run` returns to `MAIN`; the process falls through to exit.

## Error paths

### LiteLLM is importable

_Covers:_ seq 49, `LITELLM_GUARD` → `MAIN` (outcome: error)

`importlib.util.find_spec("litellm")` returning a spec means a LiteLLM install
contaminated the pinned environment. `main` raises
`GatewayError("litellm_must_not_be_installed", 500)` before any config is read
(`bootstrap.py:89-90`).

### Configuration is invalid or an environment reference is missing

_Covers:_ seq 50, `LOAD_CONFIG` → `MAIN` (outcome: error)

`load_config` wraps `OSError`, `ValueError`, `TypeError`, and pydantic
`ValidationError` into `GatewayError("invalid_configuration", 500)` without
echoing values (`adapters/config.py:189-197`). `_resolve` raises
`GatewayError("missing_configuration_environment", 500)` when a referenced
variable is unset or empty (`adapters/config.py:176-186`).

### Sanitized startup_failed and non-zero exit

_Covers:_ seq 51, `MAIN` → `JSON_EVENTS` (outcome: error); seq 52, `JSON_EVENTS` → `STDOUT` (outcome: error); seq 53, `MAIN` → `OPERATOR` (outcome: error)

The single `except GatewayError` in `main` receives every failure above, emits
`{"event":"startup_failed","error":error.code}` through a new `JsonEvents`, and
raises `SystemExit(1) from None` so no dependency traceback leaks
(`bootstrap.py:92-94`). The operator sees one JSON record on stdout and exit code
1; no `startup` event is emitted and no server binds.

### Argument errors emit a sanitized JSON record

_Covers:_ seq 54, `ARG_PARSER` → `MAIN` (outcome: error); seq 55, `ARG_PARSER` → `STDERR` (outcome: error); seq 56, `MAIN` → `JSON_EVENTS` (outcome: error); seq 57, `MAIN` → `OPERATOR` (outcome: error)

`parser.parse_args()` now runs inside a `try` that catches `SystemExit`
(`bootstrap.py:78-83`). An unknown flag or malformed option makes argparse print
its usage/error text to `STDERR` and raise `SystemExit(2)` (seq 54-55); `main`
emits `{"event":"startup_failed","error":"invalid_arguments"}` on stdout
(seq 56) and re-raises, preserving the argparse status (seq 57). `--help` exits 0
with no failure record because only non-zero exit codes emit the event. Guarded
by `tests/test_bootstrap.py:test_main_reports_bad_arguments_as_json` and
`tests/test_bootstrap.py:test_main_help_exits_cleanly_without_a_failure_event`.

## Anomalies

### Resolved: global logging disable is scoped and restored

`silence_dependency_logs` now captures `logging.root.manager.disable`, silences
at `CRITICAL`, and returns a `DependencyLogSilencer` whose release restores the
captured level (`adapters/logging.py:DependencyLogSilencer`). The in-process
scripts (`scripts/smoke.py:main`, `scripts/benchmark.py:main`) use the scoped
`with` form, so a host process gets its logging back; the CLI discards the
handle and keeps dependency logs off for its own process lifetime by design.
Guarded by the scoped-silencing tests in `tests/test_events.py`. The seq 58 link
records the resolved state as a success note.

### Resolved: argument errors bypassed the JSON event contract

The `SystemExit` guard around `parse_args` now emits
`{"event":"startup_failed","error":"invalid_arguments"}` before re-raising
(`bootstrap.py:78-83`), so malformed invocations produce a structured stdout
record even though argparse's usage text still goes to `STDERR` (seq 54-57).

### Resolved: Uvicorn start failure skipped lifespan cleanup

`build_app` no longer allocates the client or the compressor. The lifespan
constructs and registers them once the server enters it, so a bind/start failure
leaves no pooled client and no executor to release (seq 59). Guarded by
`tests/test_bootstrap.py:test_main_start_failure_constructs_no_adapters` and
`tests/test_bootstrap.py:test_build_app_defers_adapter_construction_until_lifespan`.

## Verification

- `src/switchyard_gateway/bootstrap.py:main` — confirmed argparse options
  `--config`/`--check` parsed under `except SystemExit` with a non-zero-code
  check, the ordering of `silence_dependency_logs`, the three telemetry env
  writes, the `find_spec("litellm")` guard, the `except GatewayError` handler
  emitting `startup_failed` + `SystemExit(1)`, the `--check`
  `configuration_valid` emit, and
  `uvicorn.run(..., workers=1, access_log=False, log_config=None)`.
- `src/switchyard_gateway/bootstrap.py:build_app` — confirmed only `JsonEvents`
  and the app are created; the client, compressor, router, transport, and
  Gateway are constructed by the lifespan and registered on an `AsyncExitStack`.
- `src/switchyard_gateway/adapters/config.py:load_config` — confirmed JSONC
  parsing (`json5.loads(..., allow_duplicate_keys=False)`), `_resolve` env
  handling, exception wrapping into sanitized `GatewayError`s, and
  `Config.settings()`.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit`,
  `silence_dependency_logs`, and `DependencyLogSilencer` — confirmed one-line
  JSON serialization to a `TextIO` defaulting to `sys.stdout`, the immediate
  `logging.disable(logging.CRITICAL)` on first entry, and restoration of the
  captured `logging.root.manager.disable` level on release, including when the
  body raises.
- `tests/test_events.py` (scoped-silencing tests) — pin scoped restore, the CLI
  bare-call form, nested scopes and re-entered handles, release without entry,
  exception restore, re-enabled logging after exit, and a Hypothesis property
  over arbitrary prior disable levels.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.__init__` —
  confirmed the eager `ThreadPoolExecutor(max_workers=1)` allocation now happens
  inside the lifespan.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — confirmed the
  lifespan is stored on the FastAPI app and handlers resolve the Gateway from
  `app.state`, making the lifespan the shared CLI/app hinge.
- `tests/test_bootstrap.py` (unit) — invokes `main` for bad arguments, `--help`,
  configuration failure, the LiteLLM guard, `--check`, the default config path,
  uvicorn kwargs, and a failing `uvicorn.run`.
- `scripts/smoke.py:main` and `scripts/container_smoke.py:main` — confirmed the
  offline startup/readiness behavior; no test tier starts a real server.

## Serialization notes

### Group assignment

- `MAIN`, `ARG_PARSER`, `LITELLM_GUARD`, `BUILD_APP`, `GATEWAY`, and `LIFESPAN`
  are placed in group 3. The project legend has no dedicated bootstrap/composition
  group; group 3 is the closest fit for the composition root and the application
  core it constructs, while the concrete adapters it wires are group 4.
- `INGRESS_APP` (`create_app`) is group 2 because the legend assigns FastAPI/ASGI
  boundaries there, even though the factory lives under `adapters/`.
- `CONFIG_FILE`, `ENV`, `STDOUT`, `STDERR`, `UVICORN`, and `THREAD_POOL` are
  group 7 process/OS resources; `SWITCHYARD_SDK`/`HEADROOM_SDK` are group 5
  vendor SDKs; `HTTPX_CLIENT` is group 4 because the legend names HTTPX as an
  adapter.

### Roles

- `OPERATOR` is `initiator` only (initiator-wins): it both starts the lifecycle
  and receives the eventual return/exit, so its termination is derived from the
  incoming return edges rather than declared.
- `STDOUT` and `UVICORN` are `sink`: they are the intended server-side
  terminations (the JSON response surface and the running worker).
- `STDERR` remains `dead-end`: argparse usage text terminates there, but the
  malformed invocation is no longer off-contract because `MAIN` emits the
  `startup_failed` record on stdout (seq 56).

### Seq ordering and kind mapping

- The `--check` branch (seq 14-16) and the server branch (seq 17-46) are
  mutually exclusive but share one total order; the `--check` short-circuit is
  placed first because it terminates earliest. This exclusivity is a
  serialization limitation, not two sequential phases.
- Adapter construction (seq 23-35) is ordered after `uvicorn.run` (seq 21)
  because it happens inside the ASGI lifespan, not in `build_app`.
- Error links occupy seq 49-57, after the happy path, so outcome filters extract
  complete subgraphs without interleaving; seq 58 records the resolved
  dependency-log silencing and seq 59 the absent construction-before-ownership
  window, both as success notes.
- `note` is used for static import/dependency annotations (seq 47-48) and for
  the resolved logging (seq 58) and ownership (seq 59) notes; `event` is used
  for fire-and-forget writes to stdout and for the LiteLLM-present signal
  (seq 49).

## References

### Specs and decisions

- [README.md §Configuration](README.md) — JSONC rules, env-reference semantics, and "Configuration changes require restart/recreation".
- [README.md §Observability](README.md) — JSON records on stdout and dependency-log suppression.
- [README.md §Verification](README.md) — the offline/container verification commands the startup path feeds.
- [AGENTS.md](../../AGENTS.md) — contracts for LiteLLM absence, sanitized logs, and bootstrap ownership of adapter lifetimes.
- [CHANGELOG.md](../../CHANGELOG.md) — the lifespan-ownership, JSON-contract, and dependency-log-scope fixes under Unreleased.

### Related lifecycles

- [app-lifespan](app-lifespan.md) — shares `build_app` and the `lifespan` context manager this lifecycle starts via `uvicorn.run`.
- [chat-stream](chat-stream.md) — downstream request flow that relies on the adapters and event sink wired here.
- [health-check](health-check.md) — container readiness probe that only succeeds once this startup path has bound the server.
- [model-discovery](model-discovery.md) — downstream read flow over the same wiring.

### Code

- `src/switchyard_gateway/bootstrap.py:main` — the lifecycle entry point.
- `src/switchyard_gateway/bootstrap.py:build_app` — shared composition and lifespan hinge.
- `src/switchyard_gateway/adapters/config.py:load_config` — configuration load and validation.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents` — terminal JSON event sink.
- `src/switchyard_gateway/adapters/logging.py:silence_dependency_logs` — dependency-log silencer with restore-on-release.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor` — compression adapter and worker thread.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — FastAPI app and lifespan ownership.
- `pyproject.toml` — console-script declaration.
- `Dockerfile` — container entrypoint and command.
