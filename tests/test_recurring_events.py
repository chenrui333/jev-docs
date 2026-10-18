"""Recurring observations retain every transition without turning retries into events."""

from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_transactions import PublicSources, snapshot

import jev_docs.sync as sync_module
from jev_docs.sync import event_id, sha256_bytes, synchronize


def pages(value: str) -> dict[str, str]:
    return {
        "primitives/choice": f"# Choice {value}\n",
        "primitives/score": f"# Score {value}\n",
    }


def sources(value: str) -> PublicSources:
    upstream = PublicSources()
    upstream.pages = pages(value)
    return upstream


def restart_sync(root: Path, value: str, day: str) -> dict:
    """Use a new interpreter and fixture client, never the network or process state."""
    project = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(project / "src"), str(project / "tests"))),
        "JEVDOCS_OBSERVED_AT": f"{day}T00:00:00Z",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; from pathlib import Path; "
            "from test_transactions import PublicSources; "
            "from jev_docs.sync import synchronize, validate_tree; "
            "root = Path(sys.argv[1]); upstream = PublicSources(); "
            "upstream.pages = json.loads(sys.argv[2]); "
            "result = synchronize(root, upstream, strict=True); "
            "validate_tree(root, strict=True); print(json.dumps(result))",
            str(root),
            json.dumps(pages(value)),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def history(root: Path) -> list[dict]:
    return [
        event
        for path in sorted((root / "events").glob("*.json"))
        for event in json.loads(path.read_text()).get("events", [])
    ]


@pytest.mark.parametrize("sequence", [("A", "B", "A", "B"), ("A", "A"), ("B", "B")])
@pytest.mark.parametrize("same_day", [False, True])
def test_full_sync_recurrence_and_noop_across_process_restarts(tmp_path, sequence, same_day):
    expected_count = 0
    previous = None
    for index, value in enumerate(sequence, start=1):
        before = snapshot(tmp_path)
        before_events = history(tmp_path)
        day = f"2026-10-{1 if same_day else index:02d}"
        result = restart_sync(tmp_path, value, day)
        changed = previous is not None and previous != value
        assert result["events"] == (4 if changed else 0)
        expected_count += 4 if changed else 0
        events = history(tmp_path)
        assert len(events) == expected_count
        assert len({event["id"] for event in events}) == expected_count
        for old in before_events:
            assert next(event for event in events if event["id"] == old["id"]) == old
        if previous == value:
            assert snapshot(tmp_path) == before
        if changed:
            for name, body in before.items():
                if name.startswith(("events/", "changes/")) and Path(name).stem != day:
                    assert (tmp_path / name).read_bytes() == body
        previous = value

    if sequence == ("A", "B", "A", "B"):
        for category, entity in {(event["category"], event["entity"]) for event in events}:
            group = {
                event["previous_event_id"]: event
                for event in events
                if (event["category"], event["entity"]) == (category, entity)
            }
            chain = [group[None]]
            while chain[-1]["id"] in group:
                chain.append(group[chain[-1]["id"]])
            assert len(chain) == 3
            assert chain[0]["previous_event_id"] is None
            assert chain[1]["previous_event_id"] == chain[0]["id"]
            assert chain[2]["previous_event_id"] == chain[1]["id"]
            assert chain[2]["id"] != chain[0]["id"]
            assert chain[2]["before_sha256"] == chain[0]["before_sha256"]
            assert chain[2]["after_sha256"] == chain[0]["after_sha256"]
        first_source = next(
            event
            for event in events
            if event["category"] == "documentation"
            and event["entity"] == "docs:primitives/choice"
            and event["previous_event_id"] is None
        )
        assert first_source["id"] == event_id(
            "source:docs:primitives/choice",
            "modified",
            sha256_bytes(pages("A")["primitives/choice"].encode()),
            sha256_bytes(pages("B")["primitives/choice"].encode()),
        )


@pytest.mark.parametrize(
    ("interrupted_path", "replay_day", "replay_count"),
    [
        ("changes/2026-10-03.md", "2026-10-04", 0),
        ("events/2026-10-03.json", "2026-10-04", 0),
        ("state/api.json", "2026-10-04", 0),
    ],
)
def test_interrupted_multi_entity_promotion_replays_without_lost_history(
    tmp_path, monkeypatch, interrupted_path, replay_day, replay_count
):
    restart_sync(tmp_path, "A", "2026-10-01")
    restart_sync(tmp_path, "B", "2026-10-02")
    preserved = {
        name: body
        for name, body in snapshot(tmp_path).items()
        if name.startswith(("events/", "changes/"))
    }
    replace = sync_module.os.replace

    def interrupt(source, target):
        replace(source, target)
        if Path(target).relative_to(tmp_path).as_posix() == interrupted_path:
            raise OSError("injected promotion interruption")

    with monkeypatch.context() as patch:
        patch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
        patch.setattr(sync_module.os, "replace", interrupt)
        with pytest.raises(OSError, match="injected promotion interruption"):
            synchronize(tmp_path, sources("A"), strict=True)
    assert (tmp_path / ".jev-docs-pending/READY").is_file()
    with pytest.raises(RuntimeError, match="pending synchronization"):
        sync_module.validate_tree(tmp_path, strict=True)
    if interrupted_path.startswith(("events/", "state/")):
        assert len(history(tmp_path)) == 8
        assert (tmp_path / "changes/2026-10-03.md").is_file()
    assert restart_sync(tmp_path, "A", replay_day)["events"] == replay_count
    assert len(history(tmp_path)) == 8
    assert not (tmp_path / ".jev-docs-pending").exists()
    assert (tmp_path / "events/2026-10-03.json").is_file()
    assert not (tmp_path / "events/2026-10-04.json").exists()
    for name, body in preserved.items():
        assert (tmp_path / name).read_bytes() == body
    assert restart_sync(tmp_path, "B", "2026-10-05")["events"] == 4
    assert len(history(tmp_path)) == 12
    assert len({event["id"] for event in history(tmp_path)}) == 12
    promoted = snapshot(tmp_path)
    assert restart_sync(tmp_path, "B", "2026-10-06")["events"] == 0
    assert snapshot(tmp_path) == promoted


@pytest.mark.parametrize("interrupted_path", ["state/api.json", "events/baseline.json"])
def test_bootstrap_marker_is_promoted_last(tmp_path, monkeypatch, interrupted_path):
    replace = sync_module.os.replace

    def interrupt(source, target):
        replace(source, target)
        if Path(target).relative_to(tmp_path).as_posix() == interrupted_path:
            raise OSError("injected bootstrap interruption")

    with monkeypatch.context() as patch:
        patch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
        patch.setattr(sync_module.os, "replace", interrupt)
        with pytest.raises(OSError, match="injected bootstrap interruption"):
            synchronize(tmp_path, sources("A"), strict=True)
    assert (tmp_path / "events/baseline.json").exists() == interrupted_path.startswith("events/")
    assert restart_sync(tmp_path, "A", "2026-10-02")["events"] == 0
    assert history(tmp_path) == []


@pytest.mark.parametrize("second_clock", ["2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z"])
def test_legacy_history_order_and_chains_do_not_depend_on_new_clocks(tmp_path, second_clock):
    from jev_docs.sync import chain_events, deduplicate_events, load_history_events, source_events

    def manifest(value):
        return {
            "artifacts": [
                {"source_id": "docs:page", "source_type": "documentation", "sha256": value}
            ]
        }

    first = source_events(manifest("A"), manifest("B"), "2026-10-02T00:00:00Z")[0]
    second = source_events(manifest("B"), manifest("A"), second_clock)[0]
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    (events_dir / "baseline.json").write_text(json.dumps({"kind": "baseline"}))
    (events_dir / "2026-10-01.json").write_text(json.dumps({"events": [first]}))
    (events_dir / "2026-10-02.json").write_text(json.dumps({"events": [second]}))
    unchanged = snapshot(tmp_path)
    historical = load_history_events(events_dir)
    assert historical == [first, second]
    candidate = source_events(manifest("A"), manifest("B"), "2000-01-01T00:00:00Z")
    third = chain_events(candidate, historical)[0]
    assert third["previous_event_id"] == second["id"]
    assert third["id"] not in {first["id"], second["id"]}
    assert (
        chain_events([{**candidate[0], "observed_at": "2099-01-01T00:00:00Z"}], historical)[0]["id"]
        == third["id"]
    )
    # Deliberately put the child before its legacy predecessor in file order.
    (events_dir / "1999-01-01.json").write_text(json.dumps({"events": [third]}))
    historical = load_history_events(events_dir)
    replay = chain_events(candidate, historical)
    assert replay[0]["id"] == third["id"]
    assert replay[0]["previous_event_id"] == second["id"]
    assert deduplicate_events(replay, {event["id"] for event in historical}) == []
    fourth = chain_events(
        source_events(manifest("B"), manifest("A"), "1998-01-01T00:00:00Z"), historical
    )[0]
    assert fourth["previous_event_id"] == third["id"]
    for name, body in unchanged.items():
        assert (tmp_path / name).read_bytes() == body


def test_finite_observation_sequences_keep_recurrences_and_deduplicate_replays():
    from jev_docs.sync import chain_events, deduplicate_events, semantic_events, source_events

    def manifest(value):
        return {
            "artifacts": [{"source_id": "skill", "source_type": "documentation", "sha256": value}]
            if value is not None
            else []
        }

    for size in range(1, 6):
        for sequence in itertools.product(("A", "B", None), repeat=size):
            historical = []
            expected_count = 0
            for before, after in zip(sequence, sequence[1:], strict=False):
                candidates = source_events(manifest(before), manifest(after), "same-clock")
                candidates += semantic_events(
                    {"skill": {"commit": before}} if before is not None else {},
                    {"skill": {"commit": after}} if after is not None else {},
                    "same-clock",
                )
                old_records = copy.deepcopy(historical)
                identified = chain_events(candidates, historical)
                new_events = deduplicate_events(identified, {event["id"] for event in historical})
                assert historical == old_records
                assert len(new_events) == (2 if before != after else 0)
                historical.extend(new_events)
                expected_count += 2 if before != after else 0
                assert len(historical) == expected_count
                assert len({event["id"] for event in historical}) == expected_count
                replay = chain_events(candidates, list(reversed(historical)))
                assert deduplicate_events(replay, {event["id"] for event in historical}) == []


def test_event_before_state_replay_without_a_journal(tmp_path, monkeypatch):
    import shutil

    restart_sync(tmp_path, "A", "2026-10-01")

    def events_only(stage, root):
        for directory in ("changes", "events"):
            for source in (stage / directory).glob("*"):
                target = root / directory / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        raise OSError("legacy event-before-state interruption")

    with monkeypatch.context() as patch:
        patch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
        patch.setattr(sync_module, "promote", events_only)
        with pytest.raises(OSError, match="legacy event-before-state interruption"):
            synchronize(tmp_path, sources("B"), strict=True)
    preserved = {
        name: body
        for name, body in snapshot(tmp_path).items()
        if name.startswith(("events/", "changes/"))
    }
    assert restart_sync(tmp_path, "B", "2026-10-03")["events"] == 0
    for name, body in preserved.items():
        assert (tmp_path / name).read_bytes() == body
    assert restart_sync(tmp_path, "A", "2026-10-04")["events"] == 4
    assert restart_sync(tmp_path, "B", "2026-10-05")["events"] == 4
    assert len(history(tmp_path)) == 12


def test_concurrent_sync_fails_before_observation(tmp_path):
    from jev_docs.transactions import synchronization_lock

    with synchronization_lock(tmp_path):
        with pytest.raises(subprocess.CalledProcessError) as failure:
            restart_sync(tmp_path, "A", "2026-10-01")
    assert "another synchronization" in failure.value.stderr
    assert not (tmp_path / "state").exists()
    assert restart_sync(tmp_path, "A", "2026-10-02")["events"] == 0


@pytest.mark.parametrize(
    "unauthorized",
    [
        "../escape",
        "state/../../escape",
        "state/not-generated.json",
        "/tmp/escape",
        "events/2026-99-99.json",
        ".jev-docs.lock",
    ],
)
def test_journal_paths_are_all_validated_before_any_replacement(tmp_path, unauthorized):
    from jev_docs.transactions import recover_pending

    pending = tmp_path / ".jev-docs-pending"
    candidate = pending / "files/state/api.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate")
    (pending / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {"path": "state/api.json", "sha256": sha256_bytes(b"candidate")},
                    {"path": unauthorized, "sha256": sha256_bytes(b"candidate")},
                ],
            }
        )
    )
    (pending / "READY").touch()
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="unauthorized promotion path"):
        recover_pending(tmp_path)
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_unready_journal_is_discarded_before_fresh_observation(tmp_path):
    pending = tmp_path / ".jev-docs-pending"
    pending.mkdir()
    (pending / "manifest.json").write_text("interrupted preparation")
    assert restart_sync(tmp_path, "A", "2026-10-01")["events"] == 0
    assert not pending.exists()


def test_recovery_keeps_original_batch_before_a_different_new_observation(tmp_path, monkeypatch):
    restart_sync(tmp_path, "A", "2026-10-01")
    replace = sync_module.os.replace

    def interrupt(source, target):
        replace(source, target)
        if Path(target).relative_to(tmp_path).as_posix() == "changes/2026-10-02.md":
            raise OSError("interrupted before event")

    with monkeypatch.context() as patch:
        patch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
        patch.setattr(sync_module.os, "replace", interrupt)
        with pytest.raises(OSError, match="interrupted before event"):
            synchronize(tmp_path, sources("B"), strict=True)
    assert restart_sync(tmp_path, "A", "2026-10-03")["events"] == 4
    events = history(tmp_path)
    assert len(events) == 8
    assert {event["observed_at"] for event in events} == {
        "2026-10-02T00:00:00Z",
        "2026-10-03T00:00:00Z",
    }
    assert (tmp_path / "changes/2026-10-02.md").is_file()
    assert (tmp_path / "events/2026-10-02.json").is_file()
    assert (tmp_path / "changes/2026-10-03.md").is_file()


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("duplicate", "recorded in"),
        ("dangling", "missing predecessor"),
        ("cross-category", "crosses category or entity"),
        ("cross-entity", "crosses category or entity"),
        ("branched", "multiple successors"),
        ("cyclic", "cycle"),
    ],
)
@pytest.mark.parametrize("strict", [False, True])
def test_corrupt_cross_day_history_aborts_before_fetch_or_promotion(
    tmp_path, monkeypatch, strict, fault, message
):
    restart_sync(tmp_path, "A", "2026-10-01")
    restart_sync(tmp_path, "B", "2026-10-02")
    restart_sync(tmp_path, "A", "2026-10-03")
    first_path = tmp_path / "events/2026-10-02.json"
    first_day = json.loads(first_path.read_text())
    second_path = tmp_path / "events/2026-10-03.json"
    second_day = json.loads(second_path.read_text())
    first = next(
        event
        for event in first_day["events"]
        if (event["category"], event["entity"]) == ("documentation", "docs:primitives/choice")
    )
    second = next(
        event
        for event in second_day["events"]
        if (event["category"], event["entity"]) == ("documentation", "docs:primitives/choice")
    )
    if fault == "duplicate":
        second["id"] = first["id"]
    elif fault == "dangling":
        second["previous_event_id"] = "missing-event"
    elif fault in {"cross-category", "cross-entity"}:
        other = next(
            event
            for event in first_day["events"]
            if event["category"] == ("primitive" if fault == "cross-category" else "documentation")
            and event["entity"]
            == ("choice" if fault == "cross-category" else "docs:primitives/score")
        )
        second["previous_event_id"] = other["id"]
    elif fault == "branched":
        second_day["events"].append({**second, "id": "extra-successor"})
    elif fault == "cyclic":
        first["previous_event_id"] = second["id"]
    first_path.write_text(json.dumps(first_day))
    second_path.write_text(json.dumps(second_day))
    (tmp_path / "changes/2026-10-03.md").write_text(
        sync_module.render_daily_report("2026-10-03", second_day["events"])
    )
    before = snapshot(tmp_path)
    fetched = []
    promoted = []
    upstream = sources("B")

    class TrackingSources:
        def get(self, url):
            fetched.append(url)
            return upstream.get(url)

    def reject_promotion(stage, root):
        promoted.append((stage, root))
        pytest.fail("corrupt historical events must fail before promotion")

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-04T00:00:00Z")
    monkeypatch.setattr(sync_module, "promote", reject_promotion)
    monkeypatch.setattr(sync_module, "ROOT", tmp_path)
    assert sync_module.main(["validate", "--strict"]) == 1
    with pytest.raises(RuntimeError, match=message):
        synchronize(tmp_path, TrackingSources(), strict=strict)
    assert fetched == []
    assert promoted == []
    assert snapshot(tmp_path) == before


def test_mixed_legacy_roots_and_chains_extend_on_the_same_day(tmp_path):
    for day, value in enumerate(("A", "B", "A", "B"), start=1):
        restart_sync(tmp_path, value, f"2026-10-{day:02d}")
    # Multiple unlinked roots for an entity are legitimate pre-chain history.
    for day in ("2026-10-02", "2026-10-03"):
        path = tmp_path / f"events/{day}.json"
        data = json.loads(path.read_text())
        for event in data["events"]:
            event.pop("previous_event_id")
        path.write_text(json.dumps(data))
    preserved = {event["id"]: event for event in history(tmp_path)}
    sync_module.validate_tree(tmp_path, strict=True)
    assert restart_sync(tmp_path, "A", "2026-10-04")["events"] == 4
    assert len(history(tmp_path)) == 16
    for event in history(tmp_path):
        if event["id"] in preserved:
            assert event == preserved[event["id"]]
    assert restart_sync(tmp_path, "A", "2026-10-05")["events"] == 0


def test_staged_graph_requires_and_validates_retained_predecessors(tmp_path):
    import shutil

    previous = tmp_path / "previous"
    previous.mkdir()
    restart_sync(previous, "A", "2026-10-01")
    restart_sync(previous, "B", "2026-10-02")
    stage = tmp_path / "stage"
    shutil.copytree(previous, stage)
    restart_sync(stage, "A", "2026-10-03")
    (stage / "events/baseline.json").unlink()
    (stage / "events/2026-10-02.json").unlink()
    (stage / "changes/2026-10-02.md").unlink()
    with pytest.raises(RuntimeError, match="missing predecessor"):
        sync_module.validate_tree(stage, strict=True)
    sync_module.validate_tree(stage, strict=True, history_root=previous)
    path = stage / "events/2026-10-03.json"
    data = json.loads(path.read_text())
    data["events"][0]["previous_event_id"] = "missing-from-both-trees"
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="missing predecessor"):
        sync_module.validate_tree(stage, strict=True, history_root=previous)


def test_reused_attempt_cannot_authorize_publication_after_preflight_failure(tmp_path):
    upstream = PublicSources()
    attempt = {}
    sync_module.synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert attempt["publication_eligible"]
    attempt["last_successful_sync"] = "previous-attempt"
    guide = tmp_path / "BEST_PRACTICES.md"
    guide.write_text(guide.read_text() + "\nCorrupt rendering.\n")

    class NoFetch:
        def get(self, url):
            raise AssertionError("Preflight must fail before fetching")

    with pytest.raises(RuntimeError):
        sync_module.synchronize(tmp_path, NoFetch(), strict=True, attempt=attempt)
    assert attempt["publication_eligible"] is False
    assert "last_successful_sync" not in attempt
    assert attempt["phase"] == "recovery"
