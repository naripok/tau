# Fork Slimming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the features the maintainer does not use (RPC mode, self-update, Mistral transport, GitHub Copilot provider, site content) and keep the test suite green.

**Architecture:** Pure removals plus wiring cleanup. Cohesive feature modules are deleted outright. Shared files are edited only where a removed feature is wired in: imports, registries, catalog rows, CLI branches, tests. Generic infrastructure stays untouched: the device-code protocol surface in `oauth_types.py`, `oauth.py`, and the TUI device-code screen, and the TUI app's generic `startup_notices` display parameter.

**Tech Stack:** Python 3.12, uv, pytest, ruff, mypy (strict), Typer, Textual, httpx.

**Standards:** Apply the shared code standards in every task: DRY, minimal implementation (YAGNI), low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only, writing-developer-facing-text prose.

**Feature spec:** `docs/design/2026-08-28-fork-slimming-spec.md` (the behavioral contract)

**Proposal:** `docs/design/2026-08-28-fork-slimming-proposal.md`

---

## Commands

Run every command from the worktree root `/workspace/.worktrees/slim-fork`. The environment is installed (`uv sync` ran at baseline).

```
uv run pytest -q                        # full suite; must end green after every task
uv run pytest tests/test_cli.py -q      # single file
uv run ruff check src tests             # lint
uv run ruff format --check src tests    # format check
uv run mypy                             # strict type check over tau_ai, tau_agent, tau_coding
```

Baseline before Task 1: 1676 passed, 3 skipped. After each task: suite green, lint clean, format clean, mypy clean. Removed test suites lower the pass count; that is expected.

Key facts for every task:

- `ProviderConfigError` (`src/tau_coding/provider_config.py`, near line 57) is the existing configuration error type. Selection-time failures raise it.
- `ProviderApi` is the closed `Literal` of transport APIs (`src/tau_coding/provider_catalog.py`, near line 18). Removing a member turns any remaining reference into a mypy error.
- `create_model_provider` (`src/tau_coding/provider_runtime.py`, near line 62) is the single construction point for all provider configs: Anthropic, Codex, and the openai-compatible family with its api branches.
- Both configuration surfaces already validate against closed sets: `_provider_from_json` validates `type` (five values) and `_optional_provider_api` validates `api` (six values), both raising `ProviderConfigError`. Removing a transport from those sets produces the fail-at-selection behavior; do not add a second validation mechanism.
- Catalog files use a different surface: `effective_catalog()` loads and validates them with pydantic (`_CatalogFile`, `kind: ProviderKind`, `api: ProviderApi | None`) and raises `CatalogError`. Saved provider settings raise `ProviderConfigError`.
- Line numbers in this plan are approximate anchors. Locate edits by the named symbols, not by line number.

## Task 1: Remove self-update machinery and startup notices

**Files:**
- Delete: `src/tau_coding/updater.py` — self-update handoff
- Delete: `src/tau_coding/update_check.py` — update check, PyPI fetch, release-notes notices
- Delete: `src/tau_coding/data/release-notes/` — packaged release-notes data (whole directory)
- Delete: `tests/test_updater.py`, `tests/test_update_check.py`
- Modify: `src/tau_coding/cli.py` — remove the update wiring and add the unknown-command rejection for `update`
- Modify: `src/tau_coding/tui/app.py` — remove the update-specific `startup_update_notice` parameter from the `App` constructor and `run_tui_app`, and its `highlight="update"` handling; keep the generic `startup_notices` parameter
- Modify: `src/tau_coding/tui/state.py` — shrink the notice-highlight Literal to `"alert"`
- Modify: `src/tau_coding/tui/widgets.py` — drop the two widget branches keyed on `"update"` highlight
- Modify: `tests/test_cli.py` — remove stale notice tests (including `test_run_openai_tui_combines_release_notes_and_update_notice`, which passes the removed `update_notice=` parameter) and the `_startup_update_notice` monkeypatch suppressors from unrelated tests, add the spec scenarios
- Modify: `tests/test_tui_app.py` — prune the startup-update-notice display test
- Modify: `tests/test_package_metadata.py` — remove release-notes assertions

**Spec requirement:** REMOVED "tau update command"; REMOVED "startup update check".

**Interface:**
- `tau update`: the app registers no Typer subcommands; the first positional word doubles as an initial prompt. The positional dispatch reshapes the existing guard to reject only the single-token case: a bare `tau update` (one positional token) raises a usage error (`typer.BadParameter`, "Unknown command: update") with a non-zero exit. A multi-word prompt that starts with `update` (for example `tau update the README`) still starts a prompt session.
- `App`/`run_tui_app`: the update-specific `startup_update_notice` parameter and its `highlight="update"` handling are removed; the generic `startup_notices` display parameter stays. The `"update"` member of the notice-highlight Literal (`tui/state.py`) and the two widget branches keyed on it (`tui/widgets.py`) go with it.
- `cli.py`: remove the `update_check` import block, `from tau_coding.updater import update_tau`, the `tau update` help-text line, `_startup_update_notice()`, `update_command()`, the `update_notice` parameter threading through the launch helpers, and the release-notes notice construction.
- No new types. No new functions beyond the one rejection branch.

**Behavior:**
- No startup path imports or calls update-check, updater, or release-notes code.
- TUI startup shows no update or release-notes notice before the first user input.
- Print-mode output contains no update or release-notes notice.
- No outbound network request occurs during startup in either mode.

**Tests must prove:**
- `test_update_command_is_unknown`: `tau update` fails with the "Unknown command: update" usage error (assert the message, not only the exit code; the updater's refusal paths also exit non-zero at baseline)
- `test_update_as_prompt_word_starts_session`: `tau update the README` starts a prompt session with that initial prompt
- `test_no_startup_notice_in_tui` (in `tests/test_cli.py`): with the update-check cache pre-seeded and the env gates removed, the CLI threads no update notice into the TUI launch; monkeypatch `run_openai_tui` with a capturing no-op that accepts `*args` (the notice travels as a positional argument; mirror the existing `run_openai_tui` stubs in this file) and assert that no captured positional argument carries a `.message` attribute (avoid importing the deleted notice type). Red at baseline because a notice is threaded. The App never computes notices; it displays only what the CLI passes, so the capturing stub observes the entire producer side.
- `test_no_startup_notice_in_print_mode`: print-mode output contains no update or release-notes notice (invoke with a non-empty text-mode prompt; the CLI demands one before the notice code runs, and the notice echoes only for text output)
- `test_no_outbound_request_during_startup_tui` and `test_no_outbound_request_during_startup_print_mode`: startup makes zero outbound HTTP requests

Startup test contract: this environment sets `TAU_NO_UPDATE_CHECK=1`, so the startup tests must control the environment or they pass vacuously at baseline. Isolate home with the existing `isolate_home` helper from `tests/conftest.py`, and remove `TAU_NO_UPDATE_CHECK` and `CI` from the environment with monkeypatch. Each notice test pre-seeds the update-check cache at `Path.home() / '.tau' / 'cache' / 'update-check.json'` with a fresh timezone-aware ISO `checked_at` and `latest_version` above the current version (for example `"999.0.0"`), so the update notice fires deterministically without a live PyPI fetch; this works identically before and after the module deletion. Do not pre-seed the release-notes state file in these tests: it drives only the release-notes notice computed inside `run_openai_tui`, which the stub replaces. Zero-request tests patch the sync HTTP layer that the update check uses: `monkeypatch.setattr(httpx, "get", stub)` where `stub` records every call and raises. The update check is best-effort and swallows fetch exceptions, so the raise alone proves nothing; the test asserts zero recorded calls. Leave the update-check cache absent in the zero-request tests, so the baseline check fetches and the test fails for the stated reason. Patch `httpx.get` dynamically on the `httpx` module, not the deleted module's internal helper, so the patch survives the module deletion. The startup path must complete without a recorded call. Provider calls are not part of startup.

Test placement and mechanism: all six new tests live in `tests/test_cli.py`. The two zero-request tests monkeypatch the launch functions (`run_openai_tui`, `run_openai_print_mode`) with no-op coroutines and invoke the CLI under the HTTP patch, so only the `cli.py` startup segment is exercised. `test_no_startup_notice_in_tui` uses a capturing no-op for `run_openai_tui` (see Tests must prove). `test_no_startup_notice_in_print_mode` captures stderr and monkeypatches `run_openai_print_mode` with a no-op, so the red state is deterministic. `tests/test_tui_app.py` only prunes the stale display test: the App never computes notices, it displays only what the CLI passes, so there is no separate App-side red test.

Expected failure reasons before implementation: `tau update` currently runs the updater, the update check currently fetches release metadata, the notices currently appear, and `test_update_as_prompt_word_starts_session` fails on the current over-broad rejection (`Usage: tau update` fires for multi-word prompts today). The reshaped branch must reject only the single-token case and let multi-word prompts fall through.

**Check:** `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy` — expected: all green

- [ ] Add the six tests named in Tests must prove; run them and confirm each fails for the expected reason
- [ ] Delete the four files and the `data/release-notes/` directory listed above
- [ ] Remove the `cli.py` wiring listed in Interface, add the `update` rejection branch, and remove the update-specific TUI parameter, its stale display test, and the `"update"` highlight residue in `tui/state.py` and `tui/widgets.py`
- [ ] Remove release-notes assertions from `tests/test_package_metadata.py` and stale notice tests and `_startup_update_notice` suppressors from `tests/test_cli.py`
- [ ] Run the Check commands
- [ ] Commit: `git add -A && git commit -m "feat: remove self-update machinery and startup notices"`

## Task 2: Remove the RPC frontend mode

**Files:**
- Delete: `src/tau_coding/rpc.py` — the JSONL RPC frontend
- Delete: `tests/test_rpc.py`
- Modify: `src/tau_coding/rendering/base.py` — remove the `rpc` member from `PrintOutputMode`
- Modify: `src/tau_coding/cli.py` — remove the `RpcServer` import, the `run_openai_rpc_mode` function (its only caller is the removed rpc dispatch), the `rpc_requested` logic, the `--export cannot be combined with --mode rpc` guard, and the `rpc` mention in the `--mode` help text. The `StderrUiBridge` stays: print mode uses it.
- Modify: `tests/test_cli.py` — add the rejection scenario

**Spec requirement:** REMOVED "RPC frontend mode".

**Interface:**
- `--mode rpc`: with the `rpc` member gone from `PrintOutputMode`, Typer rejects the value as an invalid choice. The CLI keeps no rpc-specific branches.

**Behavior:**
- `tau --mode rpc` fails with a usage error that names the remaining modes: text, json, transcript.
- No module in `src/` references `tau_coding.rpc` or `PrintOutputMode.rpc`.

**Tests must prove:**
- `test_rpc_mode_is_rejected`: `--mode rpc` exits non-zero with a usage error naming text, json, and transcript

Expected failure reason before implementation: `--mode rpc` currently starts an RPC session, so the test fails on the working mode.

**Check:** `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy` — expected: all green

- [ ] Add the spec-scenario test; run it and confirm it fails for the expected reason
- [ ] Delete `src/tau_coding/rpc.py` and `tests/test_rpc.py`
- [ ] Remove the `base.py` enum member and the `cli.py` wiring listed in Interface
- [ ] Run the Check commands
- [ ] Commit: `git add -A && git commit -m "feat: remove the RPC frontend mode"`

## Task 3: Remove the Mistral transport

**Files:**
- Delete: `src/tau_ai/mistral.py` — the Mistral conversations transport
- Modify: `src/tau_ai/__init__.py` — remove the `MistralConversationsProvider` export
- Modify: `src/tau_coding/provider_catalog.py` — remove `"mistral-conversations"` from the `ProviderApi` Literal and from the `ProviderKind` Literal
- Modify: `src/tau_coding/provider_config.py` — remove the mistral branch in `_default_api_for_kind`, the mistral branch in `provider_kind`, `"mistral-conversations"` from the type allowlist in `_provider_from_json`, and `"mistral-conversations"` from `_optional_provider_api`
- Modify: `src/tau_coding/provider_runtime.py` — remove the `MistralConversationsProvider` import and the mistral dispatch branch
- Modify: `src/tau_coding/data/catalog.toml` — delete the `[[providers]]` block with `name = "mistral"` (kind `mistral-conversations`), including its nested `models`, `context_windows`, `model_metadata`, and cost tables. Keep mistral-named model rows under kept providers (OpenRouter `mistralai/*`, NVIDIA NIM, Vercel AI Gateway): they ride kept transports.
- Modify: `tests/test_provider_config.py`, `tests/test_provider_catalog.py`, `tests/test_multimodal_provider_payloads.py` — prune mistral cases, add the spec scenarios

The mistral cases in `tests/test_multimodal_provider_payloads.py` use the mistral transport; remove those cases and keep the file's other cases green.

**Spec requirement:** REMOVED "mistral-conversations transport"; ADDED "absent transport kind fails at selection"; ADDED "catalog offers only constructible providers".

**Interface:**
- `ProviderApi` and `ProviderKind` no longer contain `"mistral-conversations"`. Saved settings reject it with `ProviderConfigError` at parse time; catalog files reject it with `CatalogError` at load time. Both failures happen before any provider request. The closed Literals at those two validation surfaces are the guarantee: no mistral-kind entry survives parsing, so nothing reaches the construction fall-through.
- `create_model_provider` has no mistral branch.

**Behavior:**
- A user-authored catalog entry naming the mistral transport (as `kind` or `api`) fails at catalog load with `CatalogError` and sends no request.
- Saved provider settings with `type = "mistral-conversations"` fail parsing with `ProviderConfigError`.
- The builtin catalog contains no provider entry with the removed transport kind. Mistral-named model rows under kept providers stay.
- The provider picker lists only providers whose transport kind the runtime can construct.

**Tests must prove:**
- `test_user_catalog_rejects_removed_transport`: a catalog file entry naming the removed transport raises `CatalogError` at load; no request is sent
- `test_saved_settings_reject_removed_transport`: saved settings naming the removed transport raise `ProviderConfigError` at parse
- `test_builtin_catalog_only_constructible_kinds`: every builtin provider entry resolves through `provider_config_from_entry` without error, openai-compatible-family entries also construct through `create_model_provider` with fake credentials, and no entry uses the removed Mistral transport

Expected failure reasons before implementation: the removed transport currently parses, constructs, and appears in the catalog, so each new test fails on the behavior it removes.

**Check:** `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy` — expected: all green

- [ ] Add the three spec-scenario tests; run them and confirm each fails for the expected reason
- [ ] Delete `src/tau_ai/mistral.py`; remove the exports and wiring listed in Interface
- [ ] Prune the Mistral catalog rows and the mistral test cases from the three test files
- [ ] Run the Check commands
- [ ] Commit: `git add -A && git commit -m "feat: remove the Mistral conversations transport"`

## Task 4: Remove the GitHub Copilot provider, device poller, and headers

**Files:**
- Delete: `src/tau_coding/oauth_github_copilot.py` — the Copilot OAuth login
- Delete: `src/tau_coding/oauth_device.py` — the RFC 8628 poller (its only caller was Copilot)
- Modify: `src/tau_coding/oauth_registry.py` — remove the Copilot import and registry entry
- Modify: `src/tau_ai/openai_compatible.py` — remove the `Copilot-Vision-Request` header conditional
- Modify: `src/tau_ai/anthropic.py` — remove the `Copilot-Vision-Request` header conditional
- Modify: `src/tau_coding/data/catalog.toml` — remove the GitHub Copilot provider block and rows
- Modify tests: prune tests of the removed Copilot login, device-poller, and header behavior from `tests/test_oauth_providers.py`, `tests/test_oauth_tui.py`, `tests/test_credentials.py`, `tests/test_provider_config.py`, `tests/test_provider_catalog.py`, `tests/test_provider_runtime.py`, `tests/test_tau_ai.py`, `tests/test_tui_app.py`; add the spec scenarios. Rewrite tests that use the Copilot entry only as a fixture onto a kept provider entry (for example `builtin_provider_entry("anthropic")`); keep the TUI device-code screen tests in `tests/test_oauth_tui.py` as kept-surface coverage.

**Spec requirement:** REMOVED "GitHub Copilot provider support".

**Interface:**
- The OAuth registry lists only Anthropic and OpenAI Codex logins.
- The transports build headers that do not depend on the provider name: the Copilot special case is gone in both files.
- The device-code protocol surface stays untouched: `oauth_types.py` (`"device_code"` flow kind, `OAuthDeviceCodeInfo`, `DeviceCodeCallback`, `on_device_code`), `oauth.py` (Codex device-code rejection branch), and the TUI `_show_device_code` handler.

**Behavior:**
- The login-option list contains no GitHub Copilot entry.
- The builtin catalog contains no GitHub Copilot provider entry.
- A provider identified as GitHub Copilot over a kept transport sends no Copilot specific header.

**Tests must prove:**
- `test_copilot_login_option_is_gone`: listing the OAuth login options shows no GitHub Copilot
- `test_copilot_entry_leaves_picker`: the builtin catalog has no GitHub Copilot provider entry
- `test_copilot_request_headers_are_gone_openai_compatible`: an `OpenAICompatibleProvider` request with `provider_name="github-copilot"` and image input sets no `Copilot-Vision-Request` header
- `test_copilot_request_headers_are_gone_anthropic`: the same assertion for the anthropic transport path that carried the conditional

Header-test contract: build the provider directly from config with `provider_name="github-copilot"` (provider identity is configuration) and capture the outgoing request headers with inline `httpx.MockTransport` handlers, mirroring the existing Copilot header tests in `tests/test_tau_ai.py` (the tests this task negates).

Test placement: `tests/test_oauth_providers.py` hosts `test_copilot_login_option_is_gone`; `tests/test_provider_catalog.py` hosts `test_copilot_entry_leaves_picker`; the two header tests live next to the existing fake-HTTP payload fixtures (`tests/test_multimodal_provider_payloads.py` or `tests/test_tau_ai.py`, matching where each transport's request construction is already tested).

Expected failure reasons before implementation: the login option and catalog entry exist, and both transports currently set the header for Copilot-identified providers.

**Check:** `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy` — expected: all green

- [ ] Add the four spec-scenario tests; run them and confirm each fails for the expected reason
- [ ] Delete the two modules; remove the registry entry and both header conditionals
- [ ] Prune the Copilot catalog rows and the copilot and device-poller test cases from the eight test files
- [ ] Run the Check commands
- [ ] Commit: `git add -A && git commit -m "feat: remove the GitHub Copilot provider and headers"`

## Task 5: Structural repo cleanup and CI

**Files:**
- Delete: `website/` (whole directory), `landing.html`
- Delete: `.github/workflows/docs.yml`, `.github/workflows/publish.yml`
- Delete: `dev-notes/design/2026-08-16-transient-error-retry-proposal.md`, `dev-notes/design/2026-08-16-transient-error-retry-spec.md`, `dev-notes/design/2026-08-16-transient-error-retry-plan.md`, `dev-notes/design/2026-08-16-transient-error-retry-delta.md` (the superseded first-cycle docs; keep the `-simplified-*` set and the `dev-notes` summary)
- Modify: `.github/workflows/ci.yml` — remove the documentation-build job (Hugo and pagefind steps with `working-directory: website`); keep the test job with its lint, format, and type-check steps
- Modify: `AGENTS.md` — replace the Documentation Expectations section: substantial phases leave notes under `dev-notes/`; this fork has no website and no `website/content/` updates
- Modify: `README.md` — extend the fork-changes section with the removed-feature list and the upstream-sync procedure: merge upstream main, re-apply the removals by hand using the removed-feature list as the checklist, run the suite, commit; remove the Hugo-site and published-docs sections (`cd website`, `hugo server -D`, `twotimespi.dev`, `website/content/`), every remaining `twotimespi.dev` reference (header links, install scripts, providers-guide link), and the `tau update` upgrade instruction
- Modify: `src/tau_coding/data/docs/*.md` — strip or rewrite every pointer into `website/`: the RPC-mode claim in `cli.md`, the `website/content/` and `release-notes` pointers in `models.md`, and the website references in `architecture.md`, `tui.md`, `skills.md`, `extensions.md`, `security.md`, and `cli.md`
- Modify: `CONTRIBUTING.md` — remove the directions that send contributors into `website/`
- Modify: `dev-notes/README.md` — remove or rewrite the `website/content/` and published-site pointer (other website mentions inside historical dev-notes records stay)
- Create: `tests/test_repo_structure.py` — the single-workflow contract test

**Spec requirement:** ADDED "exactly one CI workflow remains". The website, landing page, docs, and design-doc removals are proposal scope with no runtime behavior.

**Interface:**
- `tests/test_repo_structure.py` reads the workflow files as text. No production code changes.

**Behavior:**
- Exactly one YAML workflow file exists under `.github/workflows/`.
- That workflow's steps invoke the project test suite (`uv run pytest`).
- No job in that workflow references the website, the landing page, or a documentation build.

**Tests must prove:**
- `test_exactly_one_workflow`: one file matches the `.github/workflows/*.yml` and `.yaml` globs combined
- `test_workflow_runs_test_suite`: resolve the single workflow (this assertion fails at baseline while three files exist), then assert a step in it invokes `uv run pytest`
- `test_no_dead_job`: iterating every workflow file, no job text contains `website`, `landing.html`, `hugo`, or `pagefind` (this variant fails at baseline because `ci.yml` carries the Hugo job; the token list is the proposal-derived marker set for removed repository content)

Expected failure reasons before implementation: three workflow files exist, the docs workflow exists, and `ci.yml` carries the Hugo job, so each new test fails on the structure it removes.

**Check:** `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy` — expected: all green

- [ ] Add the three structure tests; run them and confirm each fails for the expected reason
- [ ] Delete the directories and files listed above
- [ ] Remove the documentation-build job from `ci.yml`; update `AGENTS.md`, `README.md`, and the packaged and contributor docs listed above
- [ ] Run the Check commands
- [ ] Commit: `git add -A && git commit -m "chore: remove site content and non-test CI workflows"`
