from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jev_docs.sync import (
    Artifact,
    FetchError,
    HTTPClient,
    Response,
    actionable_discrepancies,
    deduplicate_events,
    derive_practices,
    docs_canonical_url,
    docs_source_id,
    event_id,
    extract_models,
    normalize_url,
    observed_state,
    parse_llms,
    parse_sitemap,
    release_provenance,
    render_best_practices,
    render_daily_report,
    rollup_events,
    semantic_events,
    sha256_bytes,
    source_events,
    synchronize,
)


def test_url_normalization_and_deduplication() -> None:
    assert (
        normalize_url("/concepts/state?x=1#fragment") == "https://docs.typesafe.ai/concepts/state"
    )
    assert (
        docs_canonical_url("https://docs.typesafe.ai/concepts/state.md")
        == "https://docs.typesafe.ai/concepts/state"
    )
    text = """
    - [State](https://docs.typesafe.ai/concepts/state.md)
    - [Duplicate](https://docs.typesafe.ai/concepts/state.md#x)
    - [Other](https://example.com/other.md)
    - [Relative](/primitives.md): summary
    """
    assert parse_llms(text) == [
        ("https://docs.typesafe.ai/concepts/state.md", "State", ""),
        ("https://docs.typesafe.ai/primitives.md", "Relative", "summary"),
    ]


def test_sitemap_lastmod_is_associated_with_location() -> None:
    sitemap = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://docs.typesafe.ai/state</loc><lastmod>2026-09-20</lastmod></url></urlset>"""
    assert parse_sitemap(sitemap) == {"https://docs.typesafe.ai/state": "2026-09-20"}


def test_bounded_retry_and_hash() -> None:
    attempts = 0

    class Raw:
        status = 200
        url = "https://example.test"
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"hello"

    def opener(_request, timeout):
        nonlocal attempts
        assert timeout == 2
        attempts += 1
        if attempts < 3:
            raise OSError("transient")
        return Raw()

    client = HTTPClient(opener=opener, sleep=lambda _delay: None, timeout=2)
    assert client.get("https://example.test").body == b"hello"
    assert attempts == 3
    assert (
        sha256_bytes(b"hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_permanent_failure_is_visible() -> None:
    def opener(_request, timeout):
        raise OSError("offline")

    client = HTTPClient(opener=opener, sleep=lambda _delay: None, retries=1)
    with pytest.raises(RuntimeError, match="failed to fetch"):
        client.get("https://example.test")


def test_event_ids_are_stable_and_baseline_is_empty() -> None:
    assert event_id("practice:x", "modified", "a", "b") == event_id(
        "practice:x", "modified", "a", "b"
    )
    assert semantic_events({}, {}, "2026-09-21") == []


def test_semantic_events_cover_added_modified_removed() -> None:
    old = {"practices": {"items": [{"id": "same", "summary": "old"}, {"id": "gone"}]}}
    new = {"practices": {"items": [{"id": "same", "summary": "new"}, {"id": "new"}]}}
    events = semantic_events(old, new, "2026-09-21")
    assert {(event["entity"], event["change"]) for event in events} == {
        ("same", "modified"),
        ("gone", "removed"),
        ("new", "added"),
    }


def test_generated_best_practices_is_deterministic() -> None:
    state = {
        "items": [
            {
                "id": "x",
                "category": "confidence",
                "status": "recommended",
                "summary": "Use evidence.",
                "last_verified": "2026-09-21",
                "sources": [
                    {
                        "source_id": "docs:x",
                        "url": "https://docs.typesafe.ai/x",
                        "excerpt": "Evidence.",
                    }
                ],
            }
        ]
    }
    assert render_best_practices(state) == render_best_practices(json.loads(json.dumps(state)))
    assert "state/practices.json" in render_best_practices(state)


def test_fixture_serialization_has_no_wall_clock() -> None:
    fixture = Path(__file__).parent / "fixtures" / "llms.txt"
    assert fixture.read_text().startswith("# TypeSafe AI")


def test_practice_extraction_is_conservative_and_provenance_preserving() -> None:
    artifact = Artifact(
        "docs:concepts/how-to-build-with-system-one",
        "documentation",
        "https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md",
        b"Keep deterministic work in code.",
    )
    state = derive_practices(
        {artifact.source_id: artifact.content.decode()}, {artifact.source_id: artifact}, None
    )
    match = next(item for item in state["items"] if item["id"] == "deterministic-before-jev")
    unknown = next(item for item in state["items"] if item["id"] == "noul-is-yes-probability")
    assert match["status"] == "recommended"
    assert match["sources"][0]["sha256"] == artifact.sha256
    assert unknown["status"] == "unknown"


def test_practice_disappearance_becomes_unknown_not_deprecated() -> None:
    artifact = Artifact(
        "docs:concepts/how-to-build-with-system-one",
        "documentation",
        "https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md",
        b"No matching guidance.",
    )
    previous = {
        "items": [
            {
                "id": "deterministic-before-jev",
                "category": "decision-boundary",
                "status": "recommended",
                "summary": "old summary",
                "first_seen": "2026-09-01",
                "sources": [],
            }
        ]
    }
    state = derive_practices(
        {artifact.source_id: artifact.content.decode()}, {artifact.source_id: artifact}, previous
    )
    match = next(item for item in state["items"] if item["id"] == "deterministic-before-jev")
    assert match["status"] == "unknown"
    assert match["first_seen"] == "2026-09-01"
    assert match["status"] != "deprecated"


def test_failed_sync_does_not_promote_partial_state(tmp_path: Path) -> None:
    class Offline:
        def get(self, _url):
            raise FetchError("offline")

    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, client=Offline())
    assert list(tmp_path.iterdir()) == []


def manifest_from_fixture(path: Path, contents: dict[str, bytes]) -> dict:
    pages = []
    for url, body in contents.items():
        pages.append(
            {
                "source_id": docs_source_id(url),
                "source_type": "documentation",
                "canonical_url": docs_canonical_url(url),
                "sha256": sha256_bytes(body),
            }
        )
    return {"schema_version": 1, "discovery_fixture": path.name, "artifacts": pages}


def test_first_real_documentation_transaction_and_daily_rollup() -> None:
    before_path = Path(__file__).parent / "fixtures" / "llms-before.txt"
    after_path = Path(__file__).parent / "fixtures" / "llms-after.txt"
    before = {
        "https://docs.typesafe.ai/concepts/state.md": b"state-v1",
        "https://docs.typesafe.ai/primitives/choice.md": b"choice-v1",
    }
    after = {
        "https://docs.typesafe.ai/concepts/state.md": b"state-v2",
        "https://docs.typesafe.ai/primitives/noul.md": b"noul-v1",
    }
    old_manifest = manifest_from_fixture(before_path, before)
    new_manifest = manifest_from_fixture(after_path, after)
    events = source_events(old_manifest, new_manifest, "2026-09-21T00:00:00Z")
    assert {(event["entity"], event["change"]) for event in events} == {
        ("docs:concepts/state", "modified"),
        ("docs:primitives/choice", "removed"),
        ("docs:primitives/noul", "added"),
    }
    assert len({event["id"] for event in events}) == 3
    assert source_events(new_manifest, new_manifest, "2026-09-21T00:00:00Z") == []
    assert deduplicate_events(events, {events[0]["id"]}) == sorted(
        events[1:], key=lambda item: item["id"]
    )
    rolled = rollup_events([events[0]], events)
    assert len(rolled) == 3
    assert "docs:primitives/noul" in render_daily_report("2026-09-21", rolled)
    assert "docs:primitives/choice" in render_daily_report("2026-09-21", rolled)


def test_discovery_failure_cannot_infer_documentation_removal(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("last-known-good")

    class Offline:
        def get(self, _url):
            raise FetchError("temporary discovery outage")

    with pytest.raises(RuntimeError):
        synchronize(tmp_path, client=Offline())
    assert state.read_text() == "last-known-good"


def test_partial_upstream_failure_does_not_promote_staged_state(tmp_path: Path) -> None:
    marker = tmp_path / "state-marker"
    marker.write_text("last-known-good")

    class Partial:
        def get(self, url):
            if url.endswith("/llms.txt"):
                return Response(
                    url,
                    b"- [Page](https://docs.typesafe.ai/page)\n",
                    {},
                )
            raise FetchError("page fetch failed")

    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, client=Partial())
    assert marker.read_text() == "last-known-good"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state-marker"]


def test_practice_evidence_changes_are_not_semantic_changes_without_status_change() -> None:
    old = {
        "practices": {
            "items": [
                {
                    "id": "p",
                    "category": "x",
                    "status": "recommended",
                    "summary": "same",
                    "first_seen": None,
                    "sources": [{"sha256": "a"}],
                }
            ]
        }
    }
    moved = {
        "practices": {
            "items": [
                {
                    "id": "p",
                    "category": "x",
                    "status": "recommended",
                    "summary": "same",
                    "first_seen": None,
                    "sources": [{"sha256": "b"}],
                }
            ]
        }
    }
    changed = {
        "practices": {
            "items": [
                {
                    "id": "p",
                    "category": "x",
                    "status": "unknown",
                    "summary": "same",
                    "first_seen": None,
                    "sources": [],
                }
            ]
        }
    }
    assert semantic_events(old, moved, "2026-09-21") == []
    assert semantic_events(old, changed, "2026-09-21")[0]["change"] == "modified"


def test_observation_timestamp_changes_only_with_observed_content(monkeypatch) -> None:
    monkeypatch.setattr(
        "jev_docs.sync.utc_now", lambda: datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
    )
    first = observed_state({"value": 1}, None)
    monkeypatch.setattr(
        "jev_docs.sync.utc_now", lambda: datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)
    )
    same = observed_state({"value": 1}, first)
    changed = observed_state({"value": 2}, first)
    assert same["observed_at"] == first["observed_at"]
    assert changed["observed_at"] == "2026-09-21T01:00:00Z"


def test_committed_source_coverage_has_unique_complete_pages() -> None:
    root = Path(__file__).parents[1]
    coverage = json.loads((root / "state/source-coverage.json").read_text())
    pages = coverage["pages"]
    assert coverage["complete"]
    assert coverage["fetched_count"] == coverage["discovered_count"]
    assert len(pages) == len({page["source_id"] for page in pages})
    assert len(pages) == len({page["canonical_url"] for page in pages})


def test_model_extraction_and_skill_commit_events() -> None:
    models = extract_models("""
| Jev 1.14 | `jev-1.14.0` |
| `jev-latest` | `jev-1.14.0` |
""")
    assert models == [
        {"kind": "alias", "id": "jev-latest", "target": "jev-1.14.0"},
        {"kind": "versioned", "id": "jev-1.14.0", "label": "Jev 1.14"},
    ]
    events = semantic_events(
        {
            "skill": {"commit": "old", "source": {"sha256": "a"}},
            "models": {"models": [{"id": "jev-1.13.0"}]},
            "sdk-python": {
                "version": "0.7.0",
                "source_tag": "v0.7.0",
                "source_commit": "old-sdk",
            },
        },
        {
            "skill": {"commit": "new", "source": {"sha256": "b"}},
            "models": {"models": [{"id": "jev-1.14.0"}]},
            "sdk-python": {
                "version": "0.8.0",
                "source_tag": "v0.8.0",
                "source_commit": "new-sdk",
            },
        },
        "2026-09-21",
    )
    assert {(event["category"], event["change"]) for event in events} == {
        ("skill", "modified"),
        ("model", "modified"),
        ("sdk", "modified"),
    }


def test_release_provenance_preserves_disagreements() -> None:
    current = release_provenance(
        "0.7.0",
        [{"name": "v0.7.0", "commit": {"sha": "commit-07"}}],
        [{"tag_name": "v0.7.0", "published_at": "2026-09-18"}],
    )
    assert current["source_tag"] == "v0.7.0"
    assert current["source_commit"] == "commit-07"
    assert current["discrepancies"] == []
    registry_ahead = release_provenance(
        "0.8.0",
        [{"name": "v0.7.0", "commit": {"sha": "commit-07"}}],
        [{"tag_name": "v0.7.0"}],
    )
    assert "registry_ahead_of_git_source" in registry_ahead["discrepancies"]
    tag_ahead = release_provenance(
        "0.7.0",
        [{"name": "v0.8.0", "commit": {"sha": "commit-08"}}],
        [{"tag_name": "v0.8.0"}],
    )
    assert "git_source_ahead_of_registry" in tag_ahead["discrepancies"]
    malformed = release_provenance("0.7.0", {"bad": True}, [{"bad": True}])
    assert "malformed_release_metadata" in malformed["discrepancies"]


def test_release_discrepancy_grace_period_is_actionable_only_when_stale() -> None:
    freshness = {
        "observed_at": "2026-09-17T00:00:00Z",
        "grace_period_days": 3,
        "python": {"status": "discrepancy"},
        "javascript": {"status": "up_to_date"},
    }
    assert actionable_discrepancies(freshness, datetime(2026, 9, 19, tzinfo=timezone.utc)) == []
    assert actionable_discrepancies(freshness, datetime(2026, 9, 21, tzinfo=timezone.utc)) == [
        "python"
    ]
