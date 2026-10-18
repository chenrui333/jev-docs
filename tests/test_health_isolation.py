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
    synchronize(tmp_path, upstream, strict=True)
    before = snapshot(tmp_path)
    upstream.fail_page = True
    attempt = {}
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert not attempt.get("publication_eligible")
    assert snapshot(tmp_path) == before
