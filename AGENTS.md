# AGENTS.md

Ops and config guide for AI agents working in this repo. User-facing docs
live in `README.md` (quickstart, setup reference, troubleshooting) — read
the relevant section there first; this file covers only what README and the
configs don't spell out: the contracts, the task recipes, and the release
process.

## Map

- `compose.yaml` — services `litellm` + `db` (postgres) + `headroom`
  (compression sidecar). Env comes from `.env` (gitignored; copy
  `.env.example`).
- `profiles/stage/` — the only profile: `litellm.yaml` (model inventory,
  plugin + guardrail wiring) and `switchyard.toml` (routing policy).
- `plugins/stage_scoped.py` — shim registered as the router plugin; delegates
  to the upstream TOML-built plugin only for the configured pair.
- `Dockerfile` / `Dockerfile.headroom` — image builds (Switchyard wheel /
  headroom-ai pin). Pushing to `main` triggers `publish.yml`, which pushes
  version-pinned tags to GHCR; `compose.yaml` pulls those by default
  (`LITELLM_IMAGE` / `HEADROOM_IMAGE` override, `--build` compiles locally).
  `pyproject.toml` — uv test tooling. `tests/` — static
  (`test_config.py`) + live (`test_proxy_live.py`, `test_tools_live.py`,
  `test_headroom_guardrail.py`) checks; `tests/conftest.py` loads `.env`.

Vocabulary used everywhere here: the **pair** (cheap + expensive model
strings), **tier** (capable = expensive, efficient = cheap), **group** (the
`switchyard` model group), **shim** (`stage_scoped.py`).

## Contracts (break these and routing fails silently or loudly)

1. The pair is identified by exact model strings, and tier roles come from
   declaration order in `litellm.yaml`: capable first, efficient second.
   Same model on several endpoints must reuse the identical string and differ
   only via `api_base`.
2. LiteLLM interpolates only config values that *start with* `os.environ/`,
   and only inside mappings — never inside string lists. The `openai/`
   prefix is therefore composed in `compose.yaml`
   (`CHEAP_MODEL` / `EXPENSIVE_MODEL`); the shim matches on those same vars.
   Keep the three in sync. For the same reason, header forwarding is global
   (`general_settings`), not per-group: env-valued group names in a
   forwarding list would silently never match.
2b. Env values arrive as strings: numbers (`timeout`, token limits) coerce
   fine, but booleans do not — `model_info` is a plain TypedDict with no
   Pydantic coercion, so the string `"false"` is truthy. Capability flags
   (`supports_*`) stay literal booleans in YAML; only Pydantic-validated
   paths (e.g. guardrail `default_on`) can take bools from env.
3. The shim stage-routes only the exact pair; single-candidate groups
   (the backend-ID direct groups, or any one-deployment group) pass
   silently, and
   only multi-candidate non-pair pools warn in container logs — a mis-edited
   group degrades to plain routing instead of erroring, so check logs when
   routing looks off. Never build a second two-deployment group reusing both
   pair IDs: it would be stage-routed (roles follow its declaration order).
4. Guardrail runs *after* routing: Switchyard scores pristine client
   messages; compression never rewrites stored history. `HEADROOM_DEFAULT_ON`
   must be exactly `true`/`false`, never empty (proxy fails startup on empty).
5. `POSTGRES_PASSWORD` must stay URL-safe (`A-Za-z0-9_-`); it is embedded in
   `DATABASE_URL` (Prisma `P1013` otherwise). Rotating it needs
   `docker compose down -v` (password is baked into the pgdata volume).
6. Base image is Wolfi: BusyBox `adduser`/`addgroup`, no `useradd`, no `sh`
   under the default entrypoint (override with `--entrypoint sh`).
7. Upstream drift at the pinned `SWITCHYARD_REF`: `algorithms.random(...)`
   smoke test is stale (this repo loads the bundled TOML through the real
   loader instead) — re-verify against `random_routing_plugin.py` and
   `loader.py` when bumping the pin.

## Recipes

Done = the stated check passes.

- **Rewire models/keys/endpoints:** edit `.env` only, then
  `docker compose up -d`. Done = `x-litellm-model-name` on a test request
  names the new backend.
- **Tune routing:** edit `profiles/stage/switchyard.toml`, then
  `docker compose restart litellm`. Done = proxy healthy + full suite green.
- **Toggle compression default:** set `HEADROOM_DEFAULT_ON` true/false in
  `.env`, then `docker compose up -d` (config re-reads on recreate, not per
  request). Done = `test_headroom_guardrail.py` green.
- **Add an endpoint for an existing tier:** new `model_list` entry under the
  same group with the identical model string, own `api_base`. Done = static
  tests updated if the count changes (stage needs exactly two *unique* IDs)
  + full suite green.
- **Bump the Switchyard pin:** set `SWITCHYARD_REF`, push to `main` (publish
  rebuilds and pushes the version tag), then redeploy with
  `docker compose pull && docker compose up -d` (plain `up -d` reuses the
  locally cached tag). Re-check contract 7, run the full suite. Done =
  build-time TOML smoke test passes and all live tests green.
- **Verify anything:** `uv run --group test pytest tests/ -m "not live"`
  (static, free) then `uv run --group test pytest tests/ -m live` (spends
  real backend calls, including one expensive-tier escalation). CI
  (`.github/workflows/ci.yml`) runs the static suite, both image builds,
  compose validation, secret hygiene, and the uv lock check on every
  push/PR (docs-only changes skip CI entirely) — live tests stay manual: run locally, or dispatch
  `.github/workflows/live-tests.yml` from the Actions tab / `gh workflow run`
  (needs the `live` environment secrets; `stack` builds compose in the runner,
  `external` tests a running proxy via `PROXY_URL`).

## Developer workflow: commits, version, changelog

1. Commit with [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `test:`, `chore:` …). This is what the changelog
   is written from.
2. Bump the version in `pyproject.toml` (`[project] version` — the single
   source of version truth; nothing else carries the version) per semver,
   guided by the commit types since the last release: `fix:`/`docs:`/`test:`/
   `chore:` → patch, `feat:` → minor, `feat!:` or `BREAKING CHANGE:` footer
   → major. `0.x` versions make no stability promise.
3. Record every user-visible change under `CHANGELOG.md` `## [Unreleased]`
   (`Added` / `Changed` / `Fixed` subsections, Keep a Changelog style).
   Test-only and scaffolding changes need no entry.
4. On release: move the `Unreleased` entries under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading with the compare link, tag `vX.Y.Z`.
   Tagging is automated: pushing a version bump on `main` triggers
   `.github/workflows/release.yml`, which tags and publishes a GitHub
   release (notes taken from the matching `CHANGELOG.md` section) only for
   minor/major bumps — patch bumps land untagged and roll into the next
   release. Do not create tags or releases by hand.
   The workflow refuses to release a version with no matching non-empty
   `CHANGELOG.md` section (warning annotation, no tag), so always land the
   version bump and the `Unreleased` → versioned move in the same commit.
   A notes-only follow-up commit re-triggers the workflow (it watches
   `CHANGELOG.md` too) and completes the release.

Secrets (`.env`, keys, backend URLs) never enter git, docs, or logs —
`.env.example` carries placeholders only.
