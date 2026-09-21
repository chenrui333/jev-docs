from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_docs.sync import (
    Artifact,
    FetchError,
    HTTPClient,
    derive_practices,
    docs_canonical_url,
    event_id,
    normalize_url,
    parse_llms,
    parse_sitemap,
    render_best_practices,
    semantic_events,
    sha256_bytes,
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


def test_failed_sync_does_not_promote_partial_state(tmp_path: Path) -> None:
    class Offline:
        def get(self, _url):
            raise FetchError("offline")

    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, client=Offline())
    assert list(tmp_path.iterdir()) == []
