# Agent instructions

Read README.md for setup, architecture, migration, and verification commands.

## Contracts

- Domain, ports, and application modules import only the standard library and
  gateway core. Vendor/framework types and errors stay in adapters. Bootstrap
  owns adapter construction and shutdown.
- Route original history before compression. Routing/compression inputs are
  private copies. Preserve protected instructions, cached messages, and complete
  live tool exchanges; reject compressor changes to message structure.
- Switchyard 0.2.0 uses capture clients. Exactly one target call is allowed for
  stage routing; its synthetic completion must never become a backend call or
  client response. An SDK upgrade must pass the real adapter contract tests.
- Headroom has a shared mutable pipeline and can swallow exceptions. Serialize
  execution, bound pending work, and keep zeroed failure metrics distinct from
  proven no-savings results. Cancelled requests retain their compression slot
  until the worker actually finishes.
- Python 3.14 and base dependency installs keep LiteLLM absent. Check this in
  tests and image smoke checks; do not add proxy/all/ML extras.
- Replica selection and cooldowns stay in application policy. Run one worker.
  Retry only explicitly safe cases, once on another replica, within the selected
  model. Never replay an ambiguous read failure or a started stream. Cooldowns
  and round-robin positions are process-local and reset on restart; that
  single-worker trade-off is intentional and pinned by tests.
- Close every upstream response, including cancellation before stream iteration.
  Emit exactly one terminal event for an opened exchange. A close failure is
  recorded on the event (`close_failed` with a non-completed outcome), never
  allowed to escape and replace an already-built response.
- Logs and errors contain configured public labels and sanitized codes, never
  payloads, credentials, backend URLs, raw headers, or raw dependency exceptions.
- `.env` and `config.jsonc` stay untracked. Do not touch existing Postgres volumes.

## Verification and release

Run the offline suite, Ruff, mypy, frozen dependency checks, Compose validation,
and image smoke tests for implementation changes. Paid live tests stay explicit;
never load local backend secrets implicitly into offline tests.

The offline suite is marked `unit`, `integration`, and `smoke`. Keep new tests in
the right tier: `unit` uses fake ports and Hypothesis properties, `integration`
exercises the pinned adapters/transport/ASGI ingress, and `smoke` runs the real
adapters through the app against fake local backends. Routing and compression
changes should pass `uv run mutmut run` from `[tool.mutmut]`; treat surviving
mutants in `application.py` and `domain.py` as missing assertions unless they are
provably equivalent.

Use Conventional Commits. Version truth is `[project].version` in pyproject.toml.
Record user-visible changes in CHANGELOG.md. Breaking changes require a major
bump; features a minor bump; fixes/docs/chore a patch bump. When preparing a
minor/major release, move Unreleased notes into a matching dated version section
in the same change as the version bump. Main-branch workflows create tags and
releases automatically; do not create them manually.
