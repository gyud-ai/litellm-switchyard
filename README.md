# litellm-switchyard

A self-hosted LiteLLM proxy that routes every request through NVIDIA **Switchyard stage routing**: a cheap *efficient* tier serves routine turns, an expensive *capable* tier takes over on exploration, errors, and hard reasoning. Both tiers are generic OpenAI-compatible endpoints configured entirely via `.env`.

```
client (OpenAI SDK / coding agent)
  │  POST /v1/chat/completions  {"model": "switchyard", ...}
  ▼
litellm:4000  (LiteLLM proxy + switchyard-litellm plugin, authed, Postgres-backed)
  │  stage router: tool-error / exploration signals → capable,
  │                settled turns → efficient
  ├──► capable:   ${EXPENSIVE_API_BASE} / ${EXPENSIVE_MODEL_ID}  (cloud, $$)
  └──► efficient: ${CHEAP_API_BASE}     / ${CHEAP_MODEL_ID}      (local, cheap)
```

Response bodies keep `"model": "switchyard"`; the `x-litellm-model-name` response header shows which tier actually served the turn.

Upstream reference: `NVIDIA-NeMo/Switchyard/examples/litellm` (pins `litellm==1.97.0`). The standalone `switchyard-server` binary is demo-only, so routing runs as a LiteLLM router plugin *inside* the proxy. One local addition: `plugins/stage_scoped.py` scopes stage routing to the `switchyard` pair so other model groups (including GUI-added ones) pass through to plain LiteLLM routing untouched.

## Layout

```
Dockerfile                  # litellm:1.97.0 + Switchyard wheel (pinned ref, build-time smoke test)
compose.yaml                # litellm + postgres
.env.example -> .env        # all secrets + backend URLs (never committed)
plugins/stage_scoped.py     # scopes stage routing to the switchyard pair only
profiles/stage/litellm.yaml    # one `switchyard` group, 2 deployments (capable FIRST)
profiles/stage/switchyard.toml # stage policy (file-only, no GUI equivalent)
tests/                      # pytest suite: static config checks + live proxy checks
pyproject.toml              # test tooling (uv)
```

## Quickstart

Prerequisites: Docker with Compose v2, and [`uv`](https://docs.astral.sh/uv/) if you want to run the tests.

```bash
cp .env.example .env
# edit .env (see table below), then:
docker compose up -d --build --wait
curl -fsS http://127.0.0.1:4000/health/liveliness   # "I'm alive!"

curl -i http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"switchyard","messages":[{"role":"user","content":"Reply with the word hello."}],"max_tokens":256}'
```

Success looks like: HTTP 200, body `"model": "switchyard"`, and a `x-litellm-model-name` header naming the tier that served the turn (cheap tier on a clean first turn). Admin UI: `http://127.0.0.1:4000/ui`.

```bash
docker compose down        # stop, keep postgres data
docker compose down -v     # stop and wipe postgres data
```

## Setup reference

### `.env` variables

| Variable | Purpose |
|---|---|
| `LITELLM_MASTER_KEY` | Proxy admin key (auth for API + UI) |
| `POSTGRES_PASSWORD` | DB password. **URL-safe chars only** (`A-Za-z0-9_-`); it is embedded into `DATABASE_URL`, so `@ : / ? # %` break Prisma with `P1013` |
| `LITELLM_PORT` | Host port (default `4000`, loopback-bound) |
| `CHEAP_API_BASE` / `CHEAP_API_KEY` / `CHEAP_MODEL_ID` | Efficient tier endpoint. Bare model ID; compose prepends the `openai/` provider prefix |
| `EXPENSIVE_API_BASE` / `EXPENSIVE_API_KEY` / `EXPENSIVE_MODEL_ID` | Capable tier endpoint, same format |
| `SWITCHYARD_LITELLM_PROFILE` | Profile dir under `profiles/` (default `stage`) |
| `SWITCHYARD_REF` | Switchyard commit/tag for the Docker builder (default: pinned, tested ref) |

Changing models, keys, or endpoints later = edit `.env`, `docker compose up -d`. No YAML/TOML edits needed. (If you change `POSTGRES_PASSWORD` after first boot, use `docker compose down -v` first — Postgres bakes the initial password into its data volume.)

### Tiers and routing policy

`litellm.yaml` declares exactly **two** deployments under one `switchyard` group, and **order is the capable/efficient contract** (capable first, efficient second — LiteLLM 1.97 preserves declaration order and Switchyard relies on it). `switchyard.toml` holds only policy, no model IDs:

```toml
algorithm = "stage"
picker = "efficient_first"   # cost-first default; escalate only on signal
confidence_threshold = 0.5
recent_window = 3
```

When the capable tier takes over (from the stage-router signal table):

| Tool-result signal | Examples | Effect at 0.5 |
|---|---|---|
| Critical (hard override) | `out of memory`, `connection refused` | instant escalation |
| Hard errors | tracebacks, `ModuleNotFoundError`, timeouts, missing files | escalate once corroborated |
| Spinning / exploring | repeated failures with no reads/writes, read-only investigation | pushes toward capable |
| Tests passed + recent writes | green pytest output | pushes back to efficient |

A turn with no tool history takes the picker's default (efficient). Tune via `confidence_threshold` (lower = cheaper, higher = safer) in `profiles/stage/switchyard.toml`, then `docker compose restart litellm`.

### Multiple endpoints for the same model

Give every endpoint serving the same model the **identical** model string and differentiate only via `api_base` — LiteLLM then load-balances/fails over across them, and the router still sees one tier. Different strings (another prefix, a `-host-b` suffix) look like different tiers and break the pair.

### Client headers

LiteLLM strips unknown request headers by default. `litellm.yaml` enables `forward_client_headers_to_llm_api` for the `switchyard` group only, so any `x-*` header (e.g. `x-opencode-session`, required by some backends) passes straight to the provider — no `extra_headers` body workaround needed. `Authorization` is never forwarded by this mechanism.

### Auth, database, Admin UI

`general_settings` sets the master key, `DATABASE_URL`, and `store_model_in_db: true`, so the UI at `/ui` manages virtual keys, teams, spend tracking, and *unrelated* models without restarts. Keep the `switchyard` group itself YAML-owned: the shim logs a warning and skips stage routing (rather than failing) if the group is edited into anything but the exact pair — check `docker compose logs litellm` if routing seems off after UI edits.

### Sizing

Measured idle: proxy ~720 MiB, postgres ~70 MiB; images ~2.4 GB total. The proxy is I/O-bound (no local inference — both tiers are remote), so **2 vCPU / 4 GB RAM with a 30–40 GB disk** is comfortable, no GPU needed. Build the image elsewhere if your VM is small: the Rust/maturin build is the slow, disk-hungry step; the VM only needs to pull and run.

### Tests

```bash
uv run --group test pytest tests/ -m "not live"   # static config checks, no server
uv run --group test pytest tests/ -m live          # live proxy checks (sources .env,
                                                   # spends real backend calls)
```

Static checks pin the deployment order/count, plugin registration, TOML policy, and compose/Dockerfile wiring. Live checks assert liveliness, the model group, a chat round trip inside the pair, the cheap-default contract, tool-use loops (echo + multi-step calculator), and OOM-escalation to the expensive tier with header forwarding. Run both after any redeploy.

### Troubleshooting

- `P1013: invalid port number in database URL` → `POSTGRES_PASSWORD` has URL-breaking characters; switch to `A-Za-z0-9_-` and `down -v` + `up`.
- `model=openai/os.environ/...` in logs / cost-map warnings → LiteLLM only interpolates values that *start with* `os.environ/`; the `openai/` prefix lives in `compose.yaml`, keep it that way.
- Backend 400 about a missing session/routing header → send it as a plain HTTP header (forwarded for the `switchyard` group) or check the backend's own requirements.
- `Switchyard scope: candidate pool ... overlaps` warning → the `switchyard` group was edited (usually via UI) into something that isn't the exact pair; restore two deployments, capable first.
- `docker compose -f` … `logs litellm` is the first stop for anything else; `docker compose config` validates interpolation without starting anything.

### Updating the Switchyard pin

`SWITCHYARD_REF` (`.env`, default in `compose.yaml`) pins the upstream commit the Docker builder clones. Two known drift points when moving it: upstream's Dockerfile smoke test for `algorithms.random(...)` is stale (this repo instead loads the bundled `switchyard.toml` through the real loader at build time), and the base image is Wolfi (BusyBox `adduser`, not Debian `useradd`). After bumping, rebuild and run the full suite.

## License & attribution

Apache-2.0 — see [LICENSE](LICENSE).

This repo builds on two upstream projects:

- [NVIDIA Switchyard](https://github.com/NVIDIA-NeMo/Switchyard) (Apache-2.0) — routing algorithms and the `switchyard-litellm` plugin this image embeds; `Dockerfile` is adapted from its `examples/litellm` deployment.
- [LiteLLM](https://github.com/BerriAI/litellm) (MIT) — the proxy itself (`ghcr.io/berriai/litellm:v1.97.0` base image).

Model traffic flows to backends you configure; their terms apply.
