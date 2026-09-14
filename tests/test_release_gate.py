"""The release gate: nothing reaches a Releases page from a tree that failed.

Both workflows build installers and attach them to a release the moment a
version tag lands.  That makes the gate a property of the *job graph* rather
than of any one command, and a hand edit that drops a `needs:` line re-opens it
silently -- the run stays green, it just publishes more than it should.  These
cases read the two workflow files and hold the graph to that rule.

The workflows are parsed by hand rather than with PyYAML: the suite's
dependency set has no YAML parser, and adding one to the application's
requirements to read two CI files is a poor trade for what is a handful of
indentation rules.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# The command CI proves green on `ubuntu-24.04` (desktop.yml's matrix).  The
# whole suite is deliberately not used here: its platform-specific legs are not
# proven on Linux, and a gate that fails for a reason unrelated to the tree
# teaches people to ignore it.
TEST_COMMAND = "pytest tests/sidecar"


def jobs_of(name):
    """Map job name -> {"needs": [...], "body": "..."} for one workflow."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    except StopIteration:  # pragma: no cover - a workflow with no jobs is malformed
        pytest.fail(f"{name} has no `jobs:` block")

    jobs, current = {}, None
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            if current is not None:
                jobs[current]["body"].append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2 and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            jobs[current] = {"needs": [], "body": []}
            continue
        if current is None:
            continue
        jobs[current]["body"].append(line)
        match = re.match(r"^\s{4}needs:\s*(.+?)\s*$", line)
        if match:
            value = match.group(1).strip()
            if value.startswith("["):
                value = value.strip("[]")
            jobs[current]["needs"] = [part.strip() for part in value.split(",") if part.strip()]
    return {name_: {"needs": job["needs"], "body": "\n".join(job["body"])}
            for name_, job in jobs.items()}


def reaches(jobs, job, target):
    """True when *job* waits on *target*, directly or through another job."""
    seen, queue = set(), list(jobs[job]["needs"])
    while queue:
        name = queue.pop()
        if name == target:
            return True
        if name in seen or name not in jobs:
            continue
        seen.add(name)
        queue.extend(jobs[name]["needs"])
    return False


def gating_job(jobs, workflow):
    """The job that runs the tests, or a failure naming why there is not one."""
    runners = [name for name, job in jobs.items() if TEST_COMMAND in job["body"]]
    assert len(runners) == 1, (
        f"{workflow} should have exactly one job running `{TEST_COMMAND}`, found {runners}"
    )
    return runners[0]


@pytest.mark.parametrize(
    "workflow,publisher",
    [
        # build.yml publishes the Python application; desktop.yml the Tauri one.
        ("build.yml", "release"),
        ("desktop.yml", "release"),
    ],
)
def test_the_release_job_cannot_run_without_the_tests(workflow, publisher):
    jobs = jobs_of(workflow)
    # Guard the guard: a rename that made the lookup below miss would otherwise
    # let this pass without having checked anything.
    assert publisher in jobs, f"{workflow} has no `{publisher}` job (found {sorted(jobs)})"
    assert jobs[publisher]["needs"], f"{workflow}'s `{publisher}` job waits on nothing"
    assert reaches(jobs, publisher, gating_job(jobs, workflow)), (
        f"{workflow}'s `{publisher}` job can run without the test job having passed"
    )


def test_every_publishing_workflow_has_a_test_job_at_all():
    """`build.yml` published both apps for its whole life with no test job.

    That is the failure this case exists for: not a missing `needs:` line but a
    workflow where no job runs the tests, which no amount of graph-reading
    inside the other cases would notice.
    """
    for workflow in ("build.yml", "desktop.yml"):
        assert gating_job(jobs_of(workflow), workflow)
