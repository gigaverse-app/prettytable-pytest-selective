"""Route real Git changes through the committed lane config (pytest-selective)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pytest_selective.ci import LaneConfig, plan

ROOT = Path(__file__).resolve().parents[1]
CONFIG = LaneConfig.model_validate_json(
    (ROOT / ".github/test-lanes.json").read_text(encoding="utf-8")
)
EVERY = dict.fromkeys(CONFIG.lanes, True)
NONE = dict.fromkeys(CONFIG.lanes, False)
TESTS = {**NONE, "tests": True}
WORKFLOW = ROOT / ".github/workflows/test.yml"
GATE = "success"  # the existing aggregate job ("Test successful")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path) -> str:
    git(repo, "add", "-A")
    identity = [
        "-c",
        "user.name=routing",
        "-c",
        "user.email=routing@example.com",
        "-c",
        "commit.gpgsign=false",
    ]
    git(
        repo,
        *identity,
        "commit",
        "--quiet",
        "--no-verify",
        "--allow-empty",
        "--message",
        "routing probe",
    )
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # A copy of every tracked file, so the graph sees the real package and tests.
    git(tmp_path, "init", "--quiet")
    for path in git(ROOT, "ls-files").splitlines():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    return tmp_path


@pytest.mark.parametrize(
    "path, expected",
    [
        # Prose and files that no lane job reads.
        ("README.md", NONE),
        ("CHANGELOG.md", NONE),
        ("RELEASING.md", NONE),
        ("requirements-mypy.txt", NONE),  # the mypy environment, not the tests'
        (".pre-commit-config.yaml", NONE),
        (".github/dependabot.yml", NONE),
        (".github/labels.yml", NONE),
        (".github/workflows/lint.yml", NONE),
        (".github/workflows/release.yml", NONE),
        (".github/workflows/release-drafter.yml", NONE),
        # Source, tests and the data files tests/test_style.py reads.
        ("src/prettytable/prettytable.py", TESTS),
        ("src/prettytable/colortable.py", TESTS),
        ("tests/test_style.py", TESTS),
        ("tests/conftest.py", TESTS),
        ("tests/data/table_cjk.txt", TESTS),
        ("ci_tests/test_ci_routing.py", {**NONE, "routing": True}),
        # Environment and CI execution files.
        ("pyproject.toml", EVERY),
        ("tox.ini", EVERY),
        (".github/test-lanes.json", EVERY),
        (".github/workflows/test.yml", EVERY),
    ],
)
def test_a_change_runs_exactly_its_lanes(
    repo: Path, path: str, expected: dict[str, bool]
) -> None:
    base = commit(repo)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n# routing probe\n")
    result = plan(repo, CONFIG, base, commit(repo))
    assert result.lanes == expected, result.lane_reasons


def test_missing_history_and_full_checkpoints_run_every_lane(repo: Path) -> None:
    head = commit(repo)
    assert plan(repo, CONFIG, "0" * 40, head).lanes == EVERY  # a new branch's push
    assert plan(repo, CONFIG, head, head, full=True).lanes == EVERY


def test_the_gate_guards_every_job_and_lane_jobs_run_when_planning_fails() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "pull_request" in workflow[True]  # PyYAML reads `on:` as the boolean True
    jobs = workflow["jobs"]
    (step,) = [
        step
        for step in jobs[GATE]["steps"]
        if "validation-gate" in step.get("uses", "")
    ]
    lane_jobs = json.loads(step["with"]["lane-jobs"])
    assert set(jobs[GATE]["needs"]) == set(jobs) - {GATE}
    assert set(lane_jobs) == set(jobs) - {GATE, "select-tests"}
    assert {lane for lanes in lane_jobs.values() for lane in lanes} <= set(CONFIG.lanes)
    for job, lanes in lane_jobs.items():
        condition = " ".join(jobs[job]["if"].split())
        assert "always() && !cancelled()" in condition, job
        assert "needs.select-tests.result != 'success'" in condition, job
        assert all(
            f"fromJSON(needs.select-tests.outputs.lanes).{lane}" in condition
            for lane in lanes
        ), job
