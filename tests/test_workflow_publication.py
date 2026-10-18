"""Exercise the generated-artifact boundary without network or workflow source matching."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/sync_publication.py"


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout.decode()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "state").mkdir()
    (root / "state/models.json").write_text('{"value": 1}\n')
    (root / "README.md").write_text("Trusted authored documentation\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


def invoke(root, artifact, baseline, *extra, command="prepare", diagnostics=None):
    args = [
        sys.executable,
        str(SCRIPT),
        command,
        "--repo",
        str(root),
        "--artifact",
        str(artifact),
        "--baseline",
        baseline,
        "--run-id",
        "123",
        "--run-attempt",
        "2",
    ]
    if command == "prepare":
        args += ["--diagnostics", str(diagnostics), "--sync-exit", "0"]
    return subprocess.run([*args, *extra], capture_output=True, text=True)


def prepare(root, tmp_path, attempt=None, extra=()):
    baseline = git(root, "rev-parse", "HEAD").strip()
    (root / "state/models.json").write_text('{"value": 2}\n')
    diagnostics = tmp_path / "attempt.json"
    diagnostics.write_text(json.dumps(attempt or {"phase": "sync-complete", "result": "success"}))
    artifact = tmp_path / "artifact"
    result = invoke(root, artifact, baseline, *extra, diagnostics=diagnostics)
    return baseline, artifact, result


def restore(root):
    # Restore only this test's generated fixture, never a working repository.
    (root / "state/models.json").write_text('{"value": 1}\n')
    git(root, "add", "--", "state/models.json")


def rewrite_patch(artifact, patch):
    (artifact / "generated.patch").write_bytes(patch)
    manifest = json.loads((artifact / "manifest.json").read_text())
    manifest["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    (artifact / "manifest.json").write_text(json.dumps(manifest))


def test_successful_publication_applies_only_generated_patch(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "publication_eligible=true" in result.stdout
    restore(repository)
    applied = invoke(repository, artifact, baseline, command="apply")
    assert applied.returncode == 0, applied.stderr
    assert json.loads((repository / "state/models.json").read_text()) == {"value": 2}
    assert git(repository, "diff", "--cached", "--name-only").splitlines() == ["state/models.json"]
    assert (repository / "README.md").read_text() == "Trusted authored documentation\n"


def test_expired_health_can_publish_but_remains_failure(repository, tmp_path):
    attempt = {
        "phase": "sync-complete",
        "result": "failure",
        "publication_eligible": True,
        "health_errors": ["expired SDK reconciliation"],
    }
    baseline, artifact, result = prepare(repository, tmp_path, attempt, ("--sync-exit", "1"))
    assert result.returncode == 0, result.stderr
    assert "health_failure=true" in result.stdout
    restore(repository)
    applied = invoke(repository, artifact, baseline, command="apply")
    assert applied.returncode == 0, applied.stderr


@pytest.mark.parametrize(
    "attempt",
    [
        {"phase": "fetch", "result": "failure"},
        {"phase": "sync-complete", "result": "failure"},
        {"phase": "sync-complete", "result": "success", "publication_eligible": False},
        {"phase": "fetch", "result": "success", "publication_eligible": True},
        {"phase": "sync-complete", "result": "failure", "publication_eligible": True},
    ],
)
def test_incomplete_or_ineligible_attempt_cannot_publish(repository, tmp_path, attempt):
    _, artifact, result = prepare(repository, tmp_path, attempt)
    assert result.returncode != 0
    assert not artifact.exists()


def test_authored_changes_are_not_packaged(repository, tmp_path):
    (repository / "README.md").write_text("An untrusted edit\n")
    _, artifact, result = prepare(repository, tmp_path)
    assert result.returncode != 0
    assert not artifact.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("baseline", "a" * 40),
        ("run_id", "456"),
        ("run_attempt", "3"),
        ("publication_eligible", False),
        ("patch_sha256", "0" * 64),
    ],
)
def test_artifact_provenance_is_bound_to_this_run(repository, tmp_path, field, value):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((artifact / "manifest.json").read_text())
    manifest[field] = value
    (artifact / "manifest.json").write_text(json.dumps(manifest))
    restore(repository)
    rejected = invoke(repository, artifact, baseline, command="apply")
    assert rejected.returncode != 0
    assert not git(repository, "diff", "--cached", "--name-only")
    assert json.loads((repository / "state/models.json").read_text()) == {"value": 1}


@pytest.mark.parametrize(
    "target",
    [
        "README.md",
        "../escape.json",
        "state/../../escape.json",
        ".github/workflows/sync.yml",
        "state/evil.py",
    ],
)
def test_disallowed_patch_paths_are_rejected(repository, tmp_path, target):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    patch = (
        f"diff --git a/{target} b/{target}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{target}\n@@ -0,0 +1 @@\n+evil\n"
    ).encode()
    rewrite_patch(artifact, patch)
    restore(repository)
    rejected = invoke(repository, artifact, baseline, command="apply")
    assert rejected.returncode != 0
    assert not git(repository, "diff", "--cached", "--name-only")
    assert not (tmp_path / "escape.json").exists()


def test_symlink_patch_is_rejected(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    rewrite_patch(
        artifact,
        b"diff --git a/state/link.json b/state/link.json\nnew file mode 120000\n--- /dev/null\n+++ b/state/link.json\n@@ -0,0 +1 @@\n+../../escape\n\\ No newline at end of file\n",
    )
    restore(repository)
    rejected = invoke(repository, artifact, baseline, command="apply")
    assert rejected.returncode != 0
    assert not (repository / "state/link.json").exists()


def test_symlink_artifact_is_rejected(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    patch = artifact / "generated.patch"
    outside = tmp_path / "outside.patch"
    patch.rename(outside)
    patch.symlink_to(outside)
    restore(repository)
    assert invoke(repository, artifact, baseline, command="apply").returncode != 0


def test_symlink_destination_ancestor_is_rejected(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    restore(repository)
    outside = tmp_path / "outside"
    shutil.move(repository / "state", outside)
    (repository / "state").symlink_to(outside, target_is_directory=True)
    assert invoke(repository, artifact, baseline, command="apply").returncode != 0
    assert not git(repository, "diff", "--cached", "--name-only")


def test_oversized_artifact_is_rejected(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    with (artifact / "generated.patch").open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    restore(repository)
    assert invoke(repository, artifact, baseline, command="apply").returncode != 0


def test_moved_baseline_cannot_publish(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    restore(repository)
    git(repository, "commit", "--allow-empty", "-m", "main advanced")
    assert invoke(repository, artifact, baseline, command="apply").returncode != 0


def test_extra_artifact_entries_are_rejected(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    (artifact / "surprise").mkdir()
    restore(repository)
    assert invoke(repository, artifact, baseline, command="apply").returncode != 0


def test_outputs_use_explicit_output_file(repository, tmp_path, monkeypatch):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _, _, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "publication_eligible=true\n" in output.read_text()
    assert "health_failure=false\n" in output.read_text()
    assert os.stat(output).st_size < 1024


def test_origin_main_must_match_artifact_baseline(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    restore(repository)
    git(repository, "commit", "--allow-empty", "-m", "origin main advanced")
    advanced = git(repository, "rev-parse", "HEAD").strip()
    git(repository, "update-ref", "refs/remotes/origin/main", advanced)
    git(repository, "checkout", "--detach", baseline)
    rejected = invoke(repository, artifact, baseline, "--require-origin-main", command="apply")
    assert rejected.returncode != 0
    assert not git(repository, "diff", "--cached", "--name-only")


def test_empty_generated_patch_is_safe_noop(repository, tmp_path):
    baseline = git(repository, "rev-parse", "HEAD").strip()
    diagnostics = tmp_path / "attempt.json"
    diagnostics.write_text(json.dumps({"phase": "sync-complete", "result": "success"}))
    artifact = tmp_path / "artifact"
    packed = invoke(repository, artifact, baseline, diagnostics=diagnostics)
    assert packed.returncode == 0, packed.stderr
    applied = invoke(repository, artifact, baseline, command="apply")
    assert applied.returncode == 0, applied.stderr
    assert "changed=false" in applied.stdout


def test_generated_additions_and_deletions_can_publish(repository, tmp_path):
    baseline = git(repository, "rev-parse", "HEAD").strip()
    (repository / "state/models.json").unlink()
    (repository / "state/new-model.json").write_text("{}\n")
    diagnostics = tmp_path / "attempt.json"
    diagnostics.write_text(json.dumps({"phase": "sync-complete", "result": "success"}))
    artifact = tmp_path / "artifact"
    packed = invoke(repository, artifact, baseline, diagnostics=diagnostics)
    assert packed.returncode == 0, packed.stderr
    # Recreate the trusted checkout independently of the prepared staging index.
    target = tmp_path / "trusted"
    git(tmp_path, "clone", str(repository), str(target))
    applied = invoke(target, artifact, baseline, command="apply")
    assert applied.returncode == 0, applied.stderr
    assert not (target / "state/models.json").exists()
    assert (target / "state/new-model.json").read_text() == "{}\n"


def test_executable_generated_patch_is_rejected(repository, tmp_path):
    baseline, artifact, result = prepare(repository, tmp_path)
    assert result.returncode == 0, result.stderr
    rewrite_patch(
        artifact,
        b"diff --git a/state/evil.json b/state/evil.json\nnew file mode 100755\n--- /dev/null\n+++ b/state/evil.json\n@@ -0,0 +1 @@\n+evil\n",
    )
    restore(repository)
    assert invoke(repository, artifact, baseline, command="apply").returncode != 0
    assert not (repository / "state/evil.json").exists()


def test_trusted_tree_validation_rejects_consistent_manifest_with_invalid_content(
    repository, tmp_path
):
    source = SCRIPT.parents[2]
    for name in ("src", "sources", "state", "events", "changes"):
        shutil.copytree(
            source / name,
            repository / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    for name in (".gitignore", "BEST_PRACTICES.md", "MODEL_LIMITATIONS.md"):
        shutil.copyfile(source / name, repository / name)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "complete trusted baseline")
    baseline = git(repository, "rev-parse", "HEAD").strip()
    (repository / "BEST_PRACTICES.md").write_text("Inconsistent generated guide\n")
    diagnostics = tmp_path / "attempt.json"
    diagnostics.write_text(json.dumps({"phase": "sync-complete", "result": "success"}))
    artifact = tmp_path / "artifact"
    packed = invoke(repository, artifact, baseline, diagnostics=diagnostics)
    assert packed.returncode == 0, packed.stderr
    target = tmp_path / "trusted"
    git(tmp_path, "clone", str(repository), str(target))
    environment = dict(os.environ, PYTHONPATH=str(target / "src"), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-m", "jev_docs", "validate", "--strict"]
    valid = subprocess.run(command, cwd=target, env=environment, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr
    applied = invoke(target, artifact, baseline, command="apply")
    assert applied.returncode == 0, applied.stderr
    invalid = subprocess.run(command, cwd=target, env=environment, capture_output=True, text=True)
    assert invalid.returncode == 1
    assert "best-practices" in invalid.stderr
    assert git(target, "rev-parse", "HEAD").strip() == baseline


@pytest.mark.parametrize(
    "change",
    [
        "paired-delete",
        "baseline-delete",
        "baseline-edit",
        "record",
        "sources",
        "observed_at",
        "previous_event_id",
        "missing",
        "metadata",
        "malformed",
        "report-edit",
        "append",
        "new-day",
        "reorder",
    ],
)
def test_publication_preserves_immutable_history(repository, tmp_path, change):
    events = repository / "events"
    reports = repository / "changes"
    events.mkdir()
    reports.mkdir()
    baseline_path = events / "baseline.json"
    baseline_path.write_text('{"schema_version": 1, "kind": "baseline"}\n')
    day = events / "2026-10-02.json"
    old = [
        {
            "id": "first",
            "sources": ["public"],
            "observed_at": "legacy-clock",
            "previous_event_id": None,
        },
        {"id": "second", "sources": [], "previous_event_id": "first"},
    ]
    payload = {"schema_version": 1, "date": "2026-10-02", "events": old}
    day.write_text(json.dumps(payload))
    report = reports / "2026-10-02.md"
    report.write_text("Historical report\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "historical baseline")
    baseline = git(repository, "rev-parse", "HEAD").strip()
    original = day.read_bytes()
    candidate = json.loads(day.read_text())
    if change == "paired-delete":
        day.unlink()
        report.unlink()
    elif change == "baseline-delete":
        baseline_path.unlink()
    elif change == "baseline-edit":
        baseline_path.write_text("{}\n")
    elif change == "missing":
        candidate["events"].pop()
    elif change == "metadata":
        candidate["date"] = "2026-10-01"
    elif change == "record":
        candidate["events"][0]["id"] = "replacement"
    elif change in {"sources", "observed_at", "previous_event_id"}:
        candidate["events"][0][change] = ["changed"] if change == "sources" else "changed"
    elif change == "append":
        candidate["events"].append({"id": "third", "previous_event_id": "second"})
        report.write_text("Updated report with appended event\n")
    elif change == "new-day":
        (events / "1999-01-01.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "date": "1999-01-01",
                    "events": [{"id": "recovered", "observed_at": "earlier-clock"}],
                }
            )
        )
        (reports / "1999-01-01.md").write_text("Recovered day\n")
    elif change == "reorder":
        candidate["events"].reverse()
    elif change == "report-edit":
        report.write_text("Unsupported rewrite\n")
    if day.exists() and change != "new-day":
        day.write_text(
            "private malformed content" if change == "malformed" else json.dumps(candidate)
        )
    diagnostics = tmp_path / "attempt.json"
    diagnostics.write_text(json.dumps({"phase": "sync-complete", "result": "success"}))
    artifact = tmp_path / "artifact"
    packed = invoke(repository, artifact, baseline, diagnostics=diagnostics)
    assert packed.returncode == 0, packed.stderr
    target = tmp_path / "trusted"
    git(tmp_path, "clone", str(repository), str(target))
    before = {
        str(p.relative_to(target)): p.read_bytes()
        for p in target.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    index = (target / ".git/index").read_bytes()
    applied = invoke(target, artifact, baseline, command="apply")
    assert git(target, "rev-parse", "HEAD").strip() == baseline
    if change in {"append", "new-day", "reorder"}:
        assert applied.returncode == 0, applied.stderr
        retained = json.loads((target / "events/2026-10-02.json").read_text())
        assert all(event in retained["events"] for event in old)
        if change == "new-day":
            assert (target / "events/2026-10-02.json").read_bytes() == original
    else:
        assert applied.returncode != 0
        assert "Publication rejected:" in applied.stderr
        assert "private malformed content" not in applied.stderr
        assert (target / ".git/index").read_bytes() == index
        assert not git(target, "status", "--porcelain")
        assert before == {
            str(p.relative_to(target)): p.read_bytes()
            for p in target.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }


@pytest.mark.parametrize("policy", [None, "legacy-policy", "reviewed-contexts-v1"])
def test_changed_practice_state_cannot_downgrade_approval_policy(repository, tmp_path, policy):
    path = repository / "state/practices.json"
    original = json.dumps({"items": [], "legacy": True})
    path.write_text(original)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "trusted legacy practice state")
    candidate = {"items": [], "evidence_policy": policy}
    path.write_text(json.dumps(candidate))
    baseline, artifact, packed = prepare(repository, tmp_path)
    assert packed.returncode == 0, packed.stderr
    restore(repository)
    path.write_text(original)
    git(repository, "add", "--", "state/practices.json")
    applied = invoke(repository, artifact, baseline, command="apply")
    if policy == "reviewed-contexts-v1":
        assert applied.returncode == 0, applied.stderr
        assert json.loads(path.read_text()) == candidate
    else:
        assert applied.returncode != 0
        assert path.read_text() == original
        assert git(repository, "diff", "--cached") == ""
