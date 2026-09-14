"""The publishing graph: nothing reaches a Releases page except a tagged build.

Both workflows build installers and attach them to a release the moment a
version tag lands.  That makes the gate a property of the *job graph* rather
than of any one command, and every way the graph can go wrong is silent: a
trigger that also fires on a branch, or a `needs:` line that gets dropped,
does not turn the run red.  It publishes something nobody checked, and the
Releases page is where that is discovered.

Neither workflow runs the test suite any more -- that lives on the developer's
machine, where it can be run in full -- so what these cases hold is the graph
itself: that only a tag can publish, and that the uploader waits for the jobs
producing what it uploads.

The workflows are parsed by hand rather than with PyYAML: the suite's
dependency set has no YAML parser, and adding one to the application's
requirements to read two CI files is a poor trade for what is a handful of
indentation rules.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Each workflow that can publish, mapped to the job whose artifacts its
# uploader attaches.  The uploader has to wait on that job; there is nothing
# else ordering the two.
PUBLISHING = {
    "build.yml": "build",  # the four PyInstaller legs
    "desktop.yml": "package",  # the three bundle legs
}

# The ref a publication is allowed to come from.
GATE = "refs/tags/v"


def text_of(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def jobs_of(name):
    """Map job name -> {"needs": [...], "if": "...", "body": "..."}."""
    lines = text_of(name).splitlines()
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
            jobs[current] = {"needs": [], "if": "", "body": []}
            continue
        if current is None:
            continue
        jobs[current]["body"].append(line)

        needs = re.match(r"^\s{4}needs:\s*(.+?)\s*$", line)
        if needs:
            value = needs.group(1).strip()
            if value.startswith("["):
                value = value.strip("[]")
            jobs[current]["needs"] = [part.strip() for part in value.split(",") if part.strip()]

        condition = re.match(r"^\s{4}if:\s*(.+?)\s*$", line)
        if condition:
            jobs[current]["if"] = condition.group(1).strip()

    return {
        name_: {"needs": job["needs"], "if": job["if"], "body": "\n".join(job["body"])}
        for name_, job in jobs.items()
    }


def triggers_of(name):
    """Map event name -> its settings, for one workflow's `on:` block."""
    lines = text_of(name).splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "on:")
    except StopIteration:  # pragma: no cover - a workflow that never runs
        pytest.fail(f"{name} has no `on:` block")

    events, current = {}, None
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2 and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            events[current] = []
            continue
        if current is not None:
            events[current].append(line.strip())
    return {event: "\n".join(body) for event, body in events.items()}


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


def uploading_jobs(jobs):
    """Jobs that create or upload to a release, found by what they run."""
    return sorted(name for name, job in jobs.items() if "gh release" in job["body"])


@pytest.mark.parametrize("workflow", sorted(PUBLISHING))
def test_only_a_tag_lets_this_workflow_run_at_all(workflow):
    """A branch push or a pull request would make every commit a candidate.

    The test suite used to be what a branch push ran here; with it gone, the
    only thing keeping a push from building three platforms' installers is this
    trigger list, so it is worth being the exact list rather than a subset.
    """
    events = triggers_of(workflow)
    assert set(events) == {"push", "workflow_dispatch"}, (
        f"{workflow} runs on {sorted(events)}; only a tag push and a manual dispatch may start it"
    )
    assert "tags:" in events["push"], f"{workflow} does not limit its push trigger to tags"
    assert "branches:" not in events["push"], (
        f"{workflow} also pushes on branches, which would build (and can publish) on every commit"
    )


@pytest.mark.parametrize("workflow", sorted(PUBLISHING))
def test_nothing_that_touches_a_release_can_run_off_a_tag(workflow):
    """`workflow_dispatch` is why the job-level condition matters, not the trigger.

    A dispatch on a branch starts the workflow, so the `if:` on each job that
    writes to a release is the only thing standing between a hand-run and a
    published one.  This is the case that notices an `if:` going missing.
    """
    jobs = jobs_of(workflow)
    uploaders = uploading_jobs(jobs)
    assert uploaders, f"{workflow} has no job that touches a release (found {sorted(jobs)})"
    for name in uploaders:
        assert GATE in jobs[name]["if"], (
            f"{workflow}'s `{name}` job writes to a release but is not gated on `{GATE}`"
        )


@pytest.mark.parametrize("workflow,producer", sorted(PUBLISHING.items()))
def test_the_uploader_waits_for_what_it_uploads(workflow, producer):
    """Guard the guard: assert the jobs exist before asserting the edge.

    A rename that made the lookup below miss would otherwise let this pass
    without having checked anything.
    """
    jobs = jobs_of(workflow)
    assert "release" in jobs, f"{workflow} has no `release` job (found {sorted(jobs)})"
    assert producer in jobs, f"{workflow} has no `{producer}` job (found {sorted(jobs)})"
    assert jobs["release"]["needs"], f"{workflow}'s `release` job waits on nothing"
    assert reaches(jobs, "release", producer), (
        f"{workflow}'s `release` job can upload before `{producer}` has produced anything"
    )


def test_the_python_uploader_waits_for_the_release_to_exist():
    """`prepare` creates the release; the uploader's first step reads it.

    `gh release view` fails outright when the release is not there, so the
    uploader would go red rather than publish -- but only if `prepare` has
    actually finished, which nothing else in that file guarantees.  The two
    jobs ran in a comfortable order by coincidence before; the edge makes it
    hold whatever the runners do.
    """
    jobs = jobs_of("build.yml")
    assert "prepare" in jobs, f"build.yml has no `prepare` job (found {sorted(jobs)})"
    assert reaches(jobs, "release", "prepare"), (
        "build.yml's `release` job can start before `prepare` has created the release"
    )


def test_the_tag_is_still_checked_against_the_version_it_should_carry():
    """The one release guard no other case here would notice.

    The uploader writes the tag into the updater manifest as the version.  A tag
    that disagrees with `internal/version.py` therefore ships a manifest whose
    version is not the one the app reports, and every client's update check
    fails silently from then on -- nothing on the release page shows it.
    """
    text = text_of("build.yml")
    assert "Verify tag matches internal version" in text, (
        "build.yml no longer checks the tag against the internal version"
    )
    assert "internal.version import __version__" in text, (
        "the tag check no longer reads internal/version.py"
    )
