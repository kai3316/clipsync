"""The publishing graph: nothing reaches a Releases page except a tagged build.

Both workflows build installers and attach them to a release the moment a
version tag lands.  That makes the gate a property of the *job graph* rather
than of any one command, and every way the graph can go wrong is silent: a
trigger that also fires on a branch, or a `needs:` line that gets dropped,
does not turn the run red.  It publishes something nobody checked, and the
Releases page is where that is discovered.

Neither publishing workflow runs the test suite: that is `test.yml`, which runs
on every branch and can publish nothing.  What these cases hold is the graph
itself -- that only a tag can publish, that the uploader waits for the jobs
producing what it uploads, and that the testing workflow stays unable to
publish.  The workflows are parsed by hand rather than with PyYAML: the suite's
dependency set has no YAML parser, and adding one to the application's
requirements to read three CI files is a poor trade for what is a handful of
indentation rules.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Each workflow that can publish, mapped to the job whose artifacts its
# uploader attaches.  The uploader has to wait on that job; there is nothing
# else ordering the two.
#
# `build.yml` is absent on purpose.  It used to be here for its `build` job --
# the four PyInstaller legs that produced the previous Python application's
# binaries -- and that application is gone.  What remains in that file is the
# release itself: `prepare` creates it and `release` publishes it, and neither
# uploads an artifact of its own.  The desktop installers arrive from
# `desktop.yml`, which is where the upload edge still has to hold.
PUBLISHING = {
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

    Only the two publishing workflows are held to this.  `test.yml` is the
    opposite case on purpose -- it runs on every branch precisely because it has
    nothing to publish -- and the case below holds that half.
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

    The check moved into `prepare` when the PyInstaller leg that used to hold it
    was deleted with the previous application.  Where it lives is not the point;
    that it runs before the release is created is, so this asserts the job
    carrying it is the one that creates the release.
    """
    jobs = jobs_of("build.yml")
    assert "prepare" in jobs, f"build.yml has no `prepare` job (found {sorted(jobs)})"
    body = jobs["prepare"]["body"]
    assert "Verify tag matches internal version" in body, (
        "build.yml no longer checks the tag against the internal version"
    )
    assert "internal.version import __version__" in body, (
        "the tag check no longer reads internal/version.py"
    )


def test_a_branch_push_still_runs_the_suite():
    """The gap the two publishing workflows left open.

    Neither of them runs the tests, and neither of them runs on a branch -- so
    before this workflow existed, the first thing that checked a commit was the
    tag that published it.  The suite has to run somewhere that a push reaches,
    or "the tests pass" is a claim about one machine.
    """
    assert (WORKFLOWS / "test.yml").exists(), "no workflow runs the suite"
    events = triggers_of("test.yml")
    assert "push" in events, "the test workflow does not run on a push"
    assert "pull_request" in events, "the test workflow does not run on a pull request"
    # Every branch, not just the default one: a feature branch is where the
    # mistakes are.
    assert "branches: ['**']" in events["push"], (
        "the test workflow only runs on some branches"
    )
    assert "tags:" not in events["push"], (
        "the test workflow is tag-only, which is the gap it exists to close"
    )
    body = text_of("test.yml")
    assert "pytest" in body, "the test workflow does not run pytest"
    assert "npm test" in body or "npm run test:web" in body, (
        "the test workflow does not run the front-end suites"
    )
    assert "npm run typecheck" in body, (
        "the test workflow skips the only check that validates RPC reply shapes"
    )


def test_the_packaging_scripts_and_the_host_agree_on_one_sidecar_shape():
    """Two shapes are buildable, and everything that chooses has to choose alike.

    A PyInstaller onefile sidecar unpacks its whole runtime into a fresh temp
    directory on every launch -- measured here at ~1004 ms against ~191 ms for
    the same application as a directory, which is what the directory is for.  The
    directory cannot go in `externalBin` (that field takes one executable), so it
    ships as a bundle *resource* and the host looks for it under `sidecar/`
    first.

    The mistake this guards is a mismatch, not a preference: staging the tree
    while the bundler is told `externalBin` (or the reverse) points the packager
    at a path nobody wrote, and it fails at the end of a long build rather than
    at the start.
    """
    workflow = text_of("desktop.yml")
    # Both decision points read the same variable.
    assert workflow.count("CLIPSYNC_SIDECAR_ONEDIR") >= 3, (
        "the staging step and the bundler config no longer read the same flag"
    )
    assert 'if [ "$RUNNER_OS" = "macOS" ] || [ -n "${CLIPSYNC_SIDECAR_ONEDIR:-}" ]' in workflow
    assert 'os.environ.get("CLIPSYNC_SIDECAR_ONEDIR"' in workflow

    bridge = (
        Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "src" / "bridge.rs"
    ).read_text(encoding="utf-8")
    # The host prefers the staged tree, and falls back to the onefile beside it.
    assert 'dir.join("sidecar").join(name)' in bridge, (
        "the host no longer looks for the sidecar directory"
    )
    assert "tree.unwrap_or_else(|| exe.with_file_name(name))" in bridge, (
        "the host no longer falls back to the onefile beside it"
    )

    sidecar_ps1 = (WORKFLOWS.parents[1] / "scripts" / "build-sidecar.ps1").read_text(
        encoding="utf-8"
    )
    assert "[switch]$OneDir" in sidecar_ps1
    assert '$env:CLIPSYNC_SIDECAR_ONEDIR = "1"' in sidecar_ps1, (
        "the local build script no longer builds the directory shape"
    )
    assert 'Join-Path $Root "desktop/src-tauri/sidecar"' in sidecar_ps1, (
        "the directory shape is no longer staged where the host looks for it"
    )


def test_the_test_workflow_cannot_publish_anything():
    """It runs on every branch, so it must have no way to touch a release.

    A workflow that both tests branch pushes and can reach a Releases page is a
    worse version of the gap it was added to close.
    """
    body = text_of("test.yml")
    assert "gh release" not in body, "the test workflow touches a release"
    assert "contents: write" not in body, "the test workflow can write to the repository"
    assert "tags:" not in body, "the test workflow has a tag trigger"
    jobs = jobs_of("test.yml")
    assert jobs, "the test workflow has no jobs"
    for name, job in jobs.items():
        assert GATE not in job["if"], f"{name} is gated on a tag, which this workflow never has"
