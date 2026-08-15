# Per-model auto-compaction percentage

The automatic compaction threshold was only reachable through the TUI flag
`--auto-compact-threshold` (absolute tokens). Local deployments with an odd
context window (e.g. a vLLM server advertising 210,511 tokens) had no way to
schedule compaction at a stable share of their window without hardcoding the
token count in a shell alias, and an absolute global value is the wrong shape
for users who switch between models of very different sizes.

`ModelCatalogMetadata` and `ProviderModelMetadata` gain an optional
`auto_compact_percent` (integer, 1..100), set per model under
`[providers.model_metadata."<model>"]` in `catalog.toml` or the saved
`providers.json` model metadata.

Threshold resolution in `CodingSession.auto_compact_token_threshold` is now:

1. `--auto-compact-threshold` (absolute tokens, unchanged)
2. the active model's `auto_compact_percent`: `window × percent / 100`,
   floored, where `window` is the active context window (live provider value
   when discovery is available, otherwise the catalog value)
3. provider live limits (`RuntimeModelLimits.effective_auto_compact_token_limit`)
4. the built-in Pi-style reserve (`window − 16384`)

When a provider reports a smaller usable window (Codex live limits), the
percent threshold is capped at `effective_context_window` so it can never be
scheduled beyond the context the provider accepts — the same safety pattern as
`effective_auto_compact_token_limit`.

Validate with:

```bash
uv run pytest tests/test_context_window.py tests/test_provider_catalog.py tests/test_provider_config.py tests/test_coding_session.py
uv run ruff check .
uv run mypy
```
