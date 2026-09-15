# Session roles: keeping subagent sessions out of `/resume`

## What was added

Sessions can now carry an optional `role` on their index record
(`SessionRecordModel.role` / `CodingSessionRecord.role`). The first role is
`"subagent"`, exported as `tau_coding.session_manager.SUBAGENT_SESSION_ROLE`.

Two user-visible behaviors follow from it:

1. `tau --mode ... --session-role subagent "<prompt>"` stamps the new
   print-mode session with the role at creation time.
2. `SessionManager.list_sessions()` hides `role == "subagent"` records by
   default. `tau sessions --all` and `list_sessions(include_subagents=True)`
   include them. Explicit id lookups (`tau --session <id>`, `tau export <id>`,
   `SessionManager.get_session`) never filter, so subagent transcripts stay
   inspectable.

## Why it exists

The `superpowers-subagent` extension dispatches work to isolated `tau` child
processes run in print mode. Print mode has always created *and indexed* a
session for every run (`create_session_exclusive`), so each delegated child
leaked a resume entry into the parent's project index. `/resume`, the
`/sessions` command, and the TUI resume autocomplete therefore filled up with
one-off child sessions nobody wants to resume.

The extension itself never reads the session index (it collects child results
from stdout JSON events), so the index entry was a pure side effect. Tagging
the record instead of skipping the index keeps the transcript addressable by
id — useful when debugging delegated work — while the default listings stay
limited to top-level sessions.

## How it maps together

- `tau_coding/session_manager.py` owns the vocabulary: the role constant,
  `validate_session_role`, the record schema field, and the default filter in
  `list_sessions`.
- `tau_coding/cli.py` owns the flag surface: `--session-role` (print mode
  only, rejected together with `--session`) and `--all` for `tau sessions`.
- `superpowers_subagent/utils.py` (`build_tau_argv`) is the only caller that
  tags children with `--session-role subagent`. Core knows nothing about
  subagents; it only knows that roles exist.

## Compatibility notes

- The schema change is additive: old indexes without `role` load as
  `role=None` (top-level), and `SessionRecordModel` ignores unknown extra
  keys, so downgrades stay safe.
- Records written before this change carry no marker, so previously indexed
  subagent sessions still appear in listings until their index lines are
  removed manually.

## How to test

```bash
uv run pytest tests/test_session_manager.py tests/test_cli.py
```

End-to-end check without provider credentials (the run fails after the
record is created, which is enough to inspect the index):

```bash
HOME=/tmp/e2e-home uv run tau --mode json --no-extensions --no-approve \
  --session-role subagent --cwd /tmp/e2e-proj "hi"
uv run python -c "from pathlib import Path; from tau_coding.session_manager import SessionManager; \
  print(SessionManager().list_sessions(Path('/tmp/e2e-proj').resolve(), include_subagents=True))"
```
