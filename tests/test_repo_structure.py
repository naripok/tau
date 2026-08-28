"""CI workflow structure contract: exactly one workflow remains, and it runs the test suite.

The fork keeps a single GitHub Actions workflow. Removed repository content
(the website, the landing page, and documentation builds) must not reappear as
CI jobs, so every job is checked against the removed-content marker tokens.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
REMOVED_CONTENT_MARKERS = ("website", "landing.html", "hugo", "pagefind")


def _workflow_files() -> list[Path]:
    return [path for pattern in WORKFLOW_GLOBS for path in sorted(ROOT.glob(pattern))]


def _job_sections(workflow_text: str) -> list[str]:
    """Split a workflow file's text into one section per job under `jobs:`.

    Job headers are the two-space-indented keys directly under the top-level
    `jobs:` key; deeper keys are indented further and block-scalar content
    cannot sit at two spaces, so the header pattern cannot match inside a job.
    """
    lines = workflow_text.splitlines()
    jobs_index = lines.index("jobs:")
    sections: list[str] = []
    current: list[str] = []
    for line in lines[jobs_index + 1 :]:
        if re.fullmatch(r"  \S.*:", line):
            if current:
                sections.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


_BLOCK_SCALAR_INDICATORS = ("", "|", "|-", "|+", ">", ">-", ">+")


def _run_step_commands(job_text: str) -> list[str]:
    """Return the command text of every `run:` step in one job section.

    Inline commands contribute their text directly; block-scalar commands
    contribute their body lines. Comments, job names, and step names do not
    produce run-step command text, so they cannot satisfy the test-suite
    check.
    """
    commands: list[str] = []
    block_indent: int | None = None
    for line in job_text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if block_indent is not None:
            if stripped and indent <= block_indent:
                block_indent = None
            else:
                commands.append(stripped)
                continue
        if stripped.startswith("run:"):
            command = stripped[len("run:") :].strip()
            if command in _BLOCK_SCALAR_INDICATORS:
                block_indent = indent
            else:
                commands.append(command)
    return commands


def test_exactly_one_workflow() -> None:
    """The `.github/workflows/` directory holds exactly one YAML workflow file."""
    assert len(_workflow_files()) == 1


def test_workflow_runs_test_suite() -> None:
    """A `run:` step inside a job of the single workflow invokes the test suite."""
    (workflow,) = _workflow_files()
    workflow_text = workflow.read_text(encoding="utf-8")
    commands = [
        command for job in _job_sections(workflow_text) for command in _run_step_commands(job)
    ]
    assert any("uv run pytest" in command for command in commands)


def test_no_dead_job() -> None:
    """No job in any workflow references removed repository content."""
    for workflow in _workflow_files():
        for job in _job_sections(workflow.read_text(encoding="utf-8")):
            for marker in REMOVED_CONTENT_MARKERS:
                assert marker not in job
