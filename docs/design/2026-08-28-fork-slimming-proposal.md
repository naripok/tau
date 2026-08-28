# Proposal: Fork Slimming

## Intent

This fork carries upstream code the maintainer does not use. Unused providers, a
headless RPC frontend, and self-update machinery add mass and hide the parts
that matter. The fork also documents one feature with two superseded design
cycles.

The maintainer's stack is narrow. Providers in use: OpenAI Codex subscription
(browser OAuth), the plain OpenAI API, OpenRouter, and a local vLLM gateway.
Two transport modules cover all four: `tau_ai/openai_compatible.py` and
`tau_ai/openai_codex.py`. The frontend is the TUI. Programmatic use runs
in-process through the extension API and a custom subagent extension with
skills. The maintainer merges upstream releases only when their release notes
show a real benefit, such as UI performance fixes or bug fixes.

The goal is a smaller, more readable fork. The maintainer merges upstream
rarely and selectively, so future merges re-apply the removals by hand. A
prune script can automate that later if the burden grows.

## Scope

**In scope:**

- Remove the RPC frontend mode from the print layer:
  `src/tau_coding/rpc.py` (798 lines), the `--mode rpc` CLI branch, the
  `rpc` print output mode, and the RPC tests.
- Remove self-update machinery: `src/tau_coding/updater.py` (387 lines) and
  `src/tau_coding/update_check.py` (379 lines), the `tau update` CLI command,
  the startup update check, the startup release-notes notice, the packaged
  `src/tau_coding/data/release-notes/` data, and the updater tests.
- Remove the Mistral transport: `src/tau_ai/mistral.py` (526 lines), the
  `mistral-conversations` mapping in `src/tau_coding/provider_runtime.py`,
  and the Mistral tests.
- Remove GitHub Copilot OAuth login: `src/tau_coding/oauth_github_copilot.py`
  (285 lines) and its registry entry. Remove the now caller-free device-flow
  poller `src/tau_coding/oauth_device.py` (109 lines). Keep the generic
  device-code surface in `oauth_types.py`, `oauth.py`, and the TUI login
  screen as an extension point.
- Strip the GitHub Copilot header conditionals from the kept transport
  files: the `Copilot-Vision-Request` header logic in
  `src/tau_ai/openai_compatible.py` and `src/tau_ai/anthropic.py`.
- Prune the Mistral and Copilot rows from
  `src/tau_coding/data/catalog.toml` (about 99 lines), so the provider picker
  offers no provider the runtime cannot construct.
- Remove `website/` (612 KB), `landing.html` (24 KB), and the workflows
  `.github/workflows/docs.yml` and `.github/workflows/publish.yml`. Keep
  `.github/workflows/ci.yml` as the regression net, and remove its
  documentation-build job, which runs Hugo against `website/`.
- Remove the four superseded first-cycle transient-retry design docs under
  `dev-notes/design/`. Keep the final `-simplified-*` set and the
  `dev-notes/2026-08-16-transient-error-retry.md` summary.
- Update `AGENTS.md` documentation expectations: notes go to `dev-notes/`
  only, because `website/content/` no longer exists in this fork.
- Update the `README.md` fork-changes section with the removed set and the
  sync procedure for future merges.

**Out of scope:**

- Behavior changes for kept providers: Anthropic with its OAuth login,
  Google, `openai_compatible`, and Codex.
- Changes to the turn-level retry system. The current central classifier and
  terminal marker lists stay.
- Changes to TUI performance code, the extension API, or skill loading.
- Changes to the catalog file format or the loader interface.
- Any upstream feature development.

## Approach

Recommended: delete the removed set now, and re-apply removals by hand at
future upstream merges. The maintainer merges rarely and selectively, so the
repeat cost stays low. The `README.md` fork-changes section records the
removed set and serves as the merge checklist. A prune script can automate
the re-application later if the burden grows.

Alternatives considered:

- Build the prune script now. Deferred: deterministic, but the maintainer
  does not want the script today.
- Hide unused providers through configuration. Rejected by the maintainer:
  no real slimming, and the code mass stays.
- Cherry-pick single upstream commits instead of whole merges. Rejected:
  picks across interconnected code and tests produce broken states. Whole
  release merges with a hand-applied checklist are cheaper and predictable.

## Impact

- Code: the files and rows listed above. About 2.5k source lines and about
  1.5k test lines leave the tree, plus 636 KB of site content.
- Tests: the corresponding test files and cases leave the suite. The
  baseline before this change is 1676 passed, 3 skipped.
- CI: only the test workflow remains.
- Docs: `dev-notes/`, `README.md`, and `AGENTS.md` change. The website and
  the landing page disappear from the repository.
- Sync workflow: future upstream merges re-apply the removals by hand, with
  the `README.md` fork-changes section as the checklist.
