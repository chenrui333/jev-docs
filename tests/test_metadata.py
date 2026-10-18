import json

from test_transactions import PublicSources, snapshot

from jev_docs.sync import Artifact, derive_practices, semantic_events, synchronize

SID = "skill:typesafe-ai"
TEXT = "Keep deterministic work in code."


def practice(text=TEXT, previous=None):
    artifacts = {SID: Artifact(SID, "github-source", "https://example.test/skill", text.encode())}
    return derive_practices({SID: text}, artifacts, previous)


def item(state):
    return next(i for i in state["items"] if i["id"] == "deterministic-before-jev")


def test_verification_tracks_evidence_revision_not_every_clock_tick(monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    first = practice()
    assert item(first)["first_seen"] == "2026-10-01"
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    assert practice(previous=first) == first
    revised = practice(TEXT + "\nNew unrelated paragraph.", first)
    assert item(revised)["last_verified"] == "2026-10-02"
    assert item(revised)["last_changed"] == "2026-10-01"
    assert semantic_events({"practices": first}, {"practices": revised}, "2026-10-02") == []
    lost = derive_practices({}, {}, revised)
    assert item(lost)["last_verified"] == "2026-10-02"
    assert item(lost)["last_changed"] == "2026-10-02"
    assert item(lost)["status"] == "unknown"


def test_legacy_unknown_first_seen_is_not_fabricated(monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    previous = practice()
    item(previous)["first_seen"] = None
    item(previous).pop("last_changed", None)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-05T00:00:00Z")
    migrated = practice(previous=previous)
    assert item(migrated)["first_seen"] is None
    assert item(migrated)["last_verified"] == item(previous)["last_verified"]
    assert semantic_events({"practices": previous}, {"practices": migrated}, "now") == []


def test_successful_sync_times_are_attempt_metadata_not_commit_churn(tmp_path, monkeypatch):
    upstream = PublicSources()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    before = snapshot(tmp_path)
    first = json.loads((tmp_path / "state/freshness.json").read_text())
    assert "last_successful_observation" not in first
    assert first["last_changed"] == "2026-10-01T00:00:00Z"
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    attempt = {}
    assert synchronize(tmp_path, upstream, strict=True, attempt=attempt)["events"] == 0
    assert attempt["last_successful_sync"] == "2026-10-03T00:00:00Z"
    assert attempt["last_successful_fetch"] == "2026-10-03T00:00:00Z"
    assert snapshot(tmp_path) == before


def test_new_unknown_practice_gets_first_seen_when_evidence_first_arrives(monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    unknown = derive_practices({}, {}, None)
    assert item(unknown)["first_seen"] is None
    assert item(unknown)["last_verified"] is None
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    supported = practice(previous=unknown)
    assert item(supported)["first_seen"] == "2026-10-03"
    assert item(supported)["last_verified"] == "2026-10-03"


def test_verification_heading_ignores_never_verified_practices(monkeypatch):
    from jev_docs.sync import render_best_practices

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    partial = practice()
    assert "2026-10-01" in render_best_practices(partial)
    unsupported = derive_practices({}, {}, None)
    assert "2026-10-01" not in render_best_practices(unsupported)
