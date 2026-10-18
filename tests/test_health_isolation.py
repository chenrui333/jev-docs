import json

import pytest
from test_transactions import PublicSources, snapshot

from jev_docs.sync import Response, synchronize, validate_tree


class DelayedRelease(PublicSources):
    delayed = False

    def get(self, url):
        if self.delayed and "pypi.org" in url:
            return Response(
                url,
                json.dumps(
                    {"info": {"version": "0.2.0"}, "releases": {"0.1.0": [], "0.2.0": []}}
                ).encode(),
                {},
            )
        return super().get(url)


def test_expired_sdk_mismatch_preserves_trusted_sdk_but_publishes_docs(tmp_path, monkeypatch):
    upstream = DelayedRelease()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    trusted = (tmp_path / "state/sdk-python.json").read_bytes()
    upstream.delayed = True
    synchronize(tmp_path, upstream, strict=True)
    assert (tmp_path / "state/sdk-python.json").read_bytes() == trusted
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-05T00:00:00Z")
    upstream.pages["independent"] = "# Independently valid evidence"
    attempt = {}
    with pytest.raises(RuntimeError, match="release discrepancy"):
        synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert attempt["publication_eligible"] is True
    assert attempt["health_errors"] == ["python"]
    assert (tmp_path / "state/sdk-python.json").read_bytes() == trusted
    coverage = json.loads((tmp_path / "state/source-coverage.json").read_text())
    assert "docs:independent" in {p["source_id"] for p in coverage["pages"]}
    validate_tree(tmp_path, strict=True)
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="release discrepancy"):
        synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == before
    upstream.delayed = False
    assert synchronize(tmp_path, upstream, strict=True)["health_errors"] == []
    validate_tree(tmp_path, strict=True)


def test_fetch_failure_never_grants_publication_eligibility(tmp_path, monkeypatch):
    upstream = DelayedRelease()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    attempt = {}
    synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert attempt["publication_eligible"]
    before = snapshot(tmp_path)
    upstream.fail_page = True
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert not attempt.get("publication_eligible")
    assert snapshot(tmp_path) == before


def test_all_expired_sdks_are_reported_and_candidate_hashes_are_checked(tmp_path, monkeypatch):
    class BothDelayed(DelayedRelease):
        def get(self, url):
            if self.delayed and "registry.npmjs.org" in url:
                return Response(url, b'{"dist-tags":{"latest":"0.2.0"}}', {})
            return super().get(url)

    upstream = BothDelayed()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    upstream.delayed = True
    synchronize(tmp_path, upstream, strict=True)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-05T00:00:00Z")
    attempt = {}
    with pytest.raises(RuntimeError, match="python, javascript"):
        synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert attempt["health_errors"] == ["python", "javascript"]
    freshness = json.loads((tmp_path / "state/freshness.json").read_text())
    assert all(freshness[k]["state_retained"] for k in ("python", "javascript"))
    freshness["python"]["candidate_provenance"][0]["sha256"] = "0" * 64
    (tmp_path / "state/freshness.json").write_text(json.dumps(freshness))
    with pytest.raises(RuntimeError, match="Candidate SDK provenance"):
        validate_tree(tmp_path, strict=True)


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_held_sdk_keeps_old_changelog_citations_while_navigation_updates(
    tmp_path, monkeypatch, kind
):
    class OneDelayed(PublicSources):
        delayed = False

        def get(self, url):
            if self.delayed and kind == "python" and "pypi.org" in url:
                return Response(url, b'{"info":{"version":"0.2.0"},"releases":{"0.2.0":[]}}', {})
            if self.delayed and kind == "javascript" and "registry.npmjs.org" in url:
                return Response(url, b'{"dist-tags":{"latest":"0.2.0"}}', {})
            return super().get(url)

    upstream = OneDelayed()
    page = f"sdk/{kind}/changelog"
    upstream.pages[page] = "## v0.1.0 (2026-09-01)\n- Original release notes."
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    sdk_path = tmp_path / f"state/sdk-{kind}.json"
    original = json.loads(sdk_path.read_text())
    original_citation = original["release_history"][0]["documented_changes_provenance"][0]
    upstream.delayed = True
    upstream.pages[page] = "## v0.1.0 (2026-09-01)\n- Rewritten mutable release notes."
    synchronize(tmp_path, upstream, strict=True)
    retained = json.loads(sdk_path.read_text())
    assert retained["release_history"][0]["documented_changes"] == ["Original release notes."]
    citation = retained["release_history"][0]["documented_changes_provenance"][0]
    sources = {
        s["source_id"]: s
        for s in json.loads((tmp_path / "state/sources.json").read_text())["artifacts"]
    }
    assert sources[citation["source_id"]]["sha256"] == original_citation["sha256"]
    assert sources[f"docs:{page}"]["sha256"] != citation["sha256"]
    validate_tree(tmp_path, strict=True)
    before = snapshot(tmp_path)
    synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == before
    retained["release_history"][0]["documented_changes_provenance"][0]["sha256"] = "0" * 64
    sdk_path.write_text(json.dumps(retained))
    with pytest.raises(RuntimeError, match="SDK changelog provenance"):
        validate_tree(tmp_path, strict=True)
