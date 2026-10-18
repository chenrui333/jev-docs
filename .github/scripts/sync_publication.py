"""Transfer generated data, never executable source, between sync and publication jobs."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_PATCH = 16 * 1024 * 1024
MAX_MANIFEST = 16 * 1024
GENERATED = re.compile(
    r"(?:BEST_PRACTICES\.md|MODEL_LIMITATIONS\.md|state/[a-z0-9-]+\.json|"
    r"sources/(?:docs\.typesafe\.ai|github\.typesafe-ai)/manifest\.json|"
    r"events/(?:baseline|\d{4}-\d{2}-\d{2})\.json|changes/\d{4}-\d{2}-\d{2}\.md)"
)


def git(repo, *args, env=None, input=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        env=env,
        check=True,
        capture_output=True,
    ).stdout


def require(condition, message):
    if not condition:
        raise ValueError(message)


def generated_path(repo, name):
    require(GENERATED.fullmatch(name), f"not an allowlisted generated path: {name!r}")
    path = repo
    for part in name.split("/"):
        path = path / part
        require(not path.is_symlink(), f"symlink destination: {name!r}")
    if path.exists():
        require(stat.S_ISREG(path.stat().st_mode), f"not a regular generated file: {name!r}")
        require(not path.stat().st_mode & 0o111, f"executable generated file: {name!r}")


def output(**values):
    text = "".join(
        f"{key}={str(value).lower() if isinstance(value, bool) else value}\n"
        for key, value in values.items()
    )
    print(text, end="")
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(text)


def baseline_matches(args):
    require(re.fullmatch(r"[0-9a-f]{40}", args.baseline), "invalid baseline SHA")
    require(
        git(args.repo, "rev-parse", "HEAD").decode().strip() == args.baseline,
        "checkout baseline changed",
    )
    if args.require_origin_main:
        require(
            git(args.repo, "rev-parse", "refs/remotes/origin/main").decode().strip()
            == args.baseline,
            "origin/main advanced or does not match intended baseline",
        )


def prepare(args):
    baseline_matches(args)
    require(args.diagnostics.stat().st_size <= MAX_MANIFEST, "oversized diagnostics")
    attempt = json.loads(args.diagnostics.read_text())
    require(attempt.get("phase") == "sync-complete", "sync did not complete promotion")
    eligible = attempt.get("publication_eligible", attempt.get("result") == "success")
    require(eligible is True, "attempt is not publication eligible")
    health_failure = args.sync_exit != 0
    if health_failure:
        require(
            attempt.get("publication_eligible") is True
            and attempt.get("result") == "failure"
            and isinstance(attempt.get("health_errors"), list)
            and bool(attempt["health_errors"]),
            "failed sync lacks completed health-isolated promotion",
        )
    else:
        require(attempt.get("result") == "success", "sync result disagrees with exit status")
    changed = set(
        git(
            args.repo, "ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z"
        ).split(b"\0")
    )
    changed.update(git(args.repo, "diff", "--cached", "--name-only", "-z").split(b"\0"))
    changed.discard(b"")
    names = sorted(name.decode("utf-8") for name in changed)
    for name in names:
        generated_path(args.repo, name)
    if names:
        git(args.repo, "add", "--all", "--", *names)
    patch = git(
        args.repo,
        "diff",
        "--cached",
        "--binary",
        "--no-renames",
        "--full-index",
        args.baseline,
        "--",
    )
    require(len(patch) <= MAX_PATCH, "generated patch exceeds 16 MiB")
    manifest = {
        "schema_version": 1,
        "baseline": args.baseline,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "publication_eligible": True,
        "health_failure": health_failure,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    args.artifact.mkdir()
    (args.artifact / "generated.patch").write_bytes(patch)
    (args.artifact / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    output(baseline=args.baseline, publication_eligible=True, health_failure=health_failure)


def read_artifact(args):
    require(not args.artifact.is_symlink() and args.artifact.is_dir(), "invalid artifact directory")
    require(
        {p.name for p in args.artifact.iterdir()} == {"manifest.json", "generated.patch"},
        "unexpected artifact entries",
    )
    for name, maximum in (("manifest.json", MAX_MANIFEST), ("generated.patch", MAX_PATCH)):
        path = args.artifact / name
        mode = path.lstat().st_mode
        require(
            stat.S_ISREG(mode) and not mode & 0o111,
            "artifact entry must be a regular non-executable file",
        )
        require(path.stat().st_size <= maximum, f"oversized artifact {name}")
    manifest = json.loads((args.artifact / "manifest.json").read_text())
    require(manifest.get("schema_version") == 1, "unsupported artifact schema")
    for field in ("baseline", "run_id", "run_attempt"):
        require(manifest.get(field) == getattr(args, field), f"artifact {field} mismatch")
    require(manifest.get("publication_eligible") is True, "artifact not eligible")
    require(type(manifest.get("health_failure")) is bool, "invalid health failure flag")
    patch = (args.artifact / "generated.patch").read_bytes()
    require(
        hashlib.sha256(patch).hexdigest() == manifest.get("patch_sha256"), "patch digest mismatch"
    )
    return patch


def history_blob(repo, revision):
    # Check object size before reading: a small patch can reference a huge blob.
    require(int(git(repo, "cat-file", "-s", revision)) <= MAX_PATCH, "oversized history")
    return git(repo, "cat-file", "blob", revision)


def history_payload(blob):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "malformed publication history")
            result[key] = value
        return result

    try:
        payload = json.loads(blob, object_pairs_hook=unique_object)
        require(isinstance(payload, dict), "malformed publication history")
        events = payload.get("events")
        require(isinstance(events, list), "malformed publication history")
        records = {}
        for event in events:
            require(isinstance(event, dict), "malformed publication history")
            identity = event.get("id")
            require(isinstance(identity, str) and bool(identity), "malformed publication history")
            require(identity not in records, "malformed publication history")
            records[identity] = json.dumps(event, sort_keys=True, allow_nan=False)
        metadata = {key: value for key, value in payload.items() if key != "events"}
        return json.dumps(metadata, sort_keys=True, allow_nan=False), records
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("malformed publication history") from None


def validate_history(repo, baseline, entries):
    appended = set()
    reports = []
    for metadata, raw_name in entries:
        name = raw_name.decode("utf-8")
        old_mode, new_mode, old_oid, new_oid, _ = metadata.lstrip(b":").split()
        if name.startswith("changes/"):
            reports.append((name, old_mode, new_mode))
        if not name.startswith("events/"):
            continue
        existed = old_mode != b"000000"
        require(new_mode != b"000000", "publication cannot delete history")
        if name == "events/baseline.json":
            require(
                not existed or old_oid == new_oid, "publication cannot rewrite baseline history"
            )
            continue
        new_metadata, new_records = history_payload(history_blob(repo, new_oid.decode()))
        if existed:
            old_metadata, old_records = history_payload(history_blob(repo, f"{baseline}:{name}"))
            require(old_metadata == new_metadata, "publication cannot rewrite history metadata")
            require(
                all(new_records.get(key) == record for key, record in old_records.items()),
                "publication cannot remove or rewrite historical events",
            )
        else:
            old_records = {}
        if new_records.keys() - old_records.keys():
            appended.add(Path(name).stem)
    for name, old_mode, new_mode in reports:
        if old_mode != b"000000":
            require(new_mode != b"000000", "publication cannot delete historical reports")
            require(
                Path(name).stem in appended,
                "publication report changes require appended events",
            )


def apply(args):
    baseline_matches(args)
    require(
        not git(args.repo, "status", "--porcelain", "--untracked-files=all"),
        "publication checkout is not clean",
    )
    patch = read_artifact(args)
    if not patch:
        output(changed=False)
        return
    # Inspect Git's parsed paths, not patch text (quoted paths and rename syntax
    # otherwise have surprising meanings). Never extract a source-provided tar.
    records = git(args.repo, "apply", "--numstat", "-z", "-", input=patch).split(b"\0")
    require(records[-1] == b"" and len(records) > 1, "patch has no generated changes")
    for record in records[:-1]:
        fields = record.split(b"\t")
        require(len(fields) == 3, "unsupported patch path encoding")
        generated_path(args.repo, fields[2].decode("utf-8"))
    # Check and inspect modes in a disposable index before touching the real
    # index or checkout. Symlinks, submodules and executable bits are not data.
    index = Path(git(args.repo, "rev-parse", "--git-path", "index").decode().strip())
    if not index.is_absolute():
        index = args.repo / index
    with tempfile.TemporaryDirectory(prefix="jev-publication-") as temporary:
        trial = Path(temporary) / "index"
        shutil.copyfile(index, trial)
        env = dict(os.environ, GIT_INDEX_FILE=str(trial))
        git(args.repo, "apply", "--cached", "--check", "-", env=env, input=patch)
        git(args.repo, "apply", "--cached", "-", env=env, input=patch)
        raw = git(
            args.repo, "diff", "--cached", "--raw", "--no-renames", "-z", args.baseline, env=env
        ).split(b"\0")
        require(raw[-1] == b"" and len(raw) % 2 == 1, "invalid patch entries")
        entries = list(zip(raw[0:-1:2], raw[1:-1:2], strict=True))
        for metadata, name in entries:
            modes = metadata.split()[:2]
            require(
                all(mode.lstrip(b":") in (b"000000", b"100644") for mode in modes),
                "unsupported generated file mode",
            )
            generated_path(args.repo, name.decode("utf-8"))
            if name == b"state/practices.json":
                # Import policy from this trusted checkout, never artifact-controlled data.
                sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
                from jev_docs.practice_reviews import (
                    PRACTICE_EVIDENCE_POLICY,
                    _historical_practice_sources,
                )

                require(modes[1] != b"000000", "practice state cannot be deleted")
                payload = json.loads(history_blob(args.repo, metadata.split()[3].decode("ascii")))
                require(
                    isinstance(payload, dict)
                    and payload.get("evidence_policy") == PRACTICE_EVIDENCE_POLICY,
                    "changed practice state must use the current evidence policy",
                )
                previous = (
                    json.loads(history_blob(args.repo, metadata.split()[2].decode("ascii")))
                    if modes[0].lstrip(b":") != b"000000"
                    else {}
                )
                prior_items = {item["id"]: item for item in previous.get("items", [])}
                require(isinstance(payload.get("items"), list), "invalid practice inventory")
                for item in payload["items"]:
                    require(
                        isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                        and isinstance(item.get("sources", []), list),
                        "invalid practice item",
                    )
                    expected = _historical_practice_sources(
                        prior_items.get(item["id"]), item.get("sources", [])
                    )
                    require(
                        item.get("historical_sources", []) == expected,
                        "historical practice citations disagree with trusted baseline",
                    )
        validate_history(args.repo, args.baseline, entries)
    git(args.repo, "apply", "--index", "--check", "-", input=patch)
    git(args.repo, "apply", "--index", "-", input=patch)
    output(changed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "apply"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--require-origin-main", action="store_true")
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--sync-exit", type=int, default=0)
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    try:
        require(args.run_id.isdecimal() and args.run_attempt.isdecimal(), "invalid run identity")
        (prepare if args.command == "prepare" else apply)(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Publication rejected: {error}\n")


if __name__ == "__main__":
    main()
