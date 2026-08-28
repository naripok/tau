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


def test_exactly_one_workflow() -> None:
    """The `.github/workflows/` directory holds exactly one YAML workflow file."""
    assert len(_workflow_files()) == 1


def test_workflow_runs_test_suite() -> None:
    """The single workflow invokes the project test suite (`uv run pytest`)."""
    (workflow,) = _workflow_files()
    assert "uv run pytest" in workflow.read_text(encoding="utf-8")


def test_no_dead_job() -> None:
    """No job in any workflow references removed repository content."""
    for workflow in _workflow_files():
        for job in _job_sections(workflow.read_text(encoding="utf-8")):
            for marker in REMOVED_CONTENT_MARKERS:
                assert marker not in job
