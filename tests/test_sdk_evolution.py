import json

import pytest
from test_transactions import PublicSources

from jev_docs.sync import parse_release_notes, python_capabilities


class SDKSource(PublicSources):
    def get(self, url):
        from jev_docs.sync import Response

        if url.endswith("docs/changelog.md"):
            return Response(
                url,
                b"## v0.7.2 (2026-09-26)\n\n- add `http2` extra to `typesafe-sdk` package\n- document `typesafe-sdk` usage with HTTP/2 support\n",
                {},
            )
        if url.endswith("pyproject.toml"):
            return Response(url, b'version = "0.7.2"\n', {})
        return super().get(url)


def test_html_changelog_and_immutable_python_capability_provenance():
    text = '<h2 id="v072">\n v0.7.2 (2026-09-26)\n</h2>\n\n* add `http2` extra to `typesafe-sdk` package\n* document `typesafe-sdk` usage with HTTP/2 support\n'
    notes = parse_release_notes(text)
    assert notes[0]["version"] == "0.7.2" and len(notes[0]["notes"]) == 2
    release = {"version": "0.7.2", "tag": "v0.7.2", "source_commit": "a" * 40}
    artifacts = {}
    args = (
        SDKSource(),
        artifacts,
        "typesafe-ai/typesafe-sdk-python",
        [release],
        {"releases": {"0.7.2": []}},
        notes,
    )
    result = python_capabilities(*args)
    assert [item["id"] for item in result] == ["python-http2-extra", "python-http2-usage"]
    assert result == python_capabilities(*args)
    assert all(item["introduced"] == "0.7.2" for item in result)
    assert all("a" * 40 in source["url"] for item in result for source in item["provenance"])
    assert (
        python_capabilities(
            SDKSource(),
            {},
            "typesafe-ai/typesafe-sdk-python",
            [{**release, "source_commit": None}],
            args[4],
            notes,
        )
        == []
    )
    assert (
        python_capabilities(
            SDKSource(), {}, "typesafe-ai/typesafe-sdk-python", [release], {}, notes
        )
        == []
    )


def test_sdk_capabilities_do_not_leak_to_javascript(tmp_path):
    import json

    from jev_docs.sync import synchronize

    synchronize(tmp_path, PublicSources(), strict=True)
    assert json.loads((tmp_path / "state/sdk-javascript.json").read_text())["capabilities"] == []
    tool = json.loads((tmp_path / "state/evaluation-tool.json").read_text())
    assert tool["source_class"] == "official_evaluation_tool"
    assert tool["source_commit"] == "b" * 40


def test_javascript_provenance_enrichment_is_not_a_release_event():
    from jev_docs.sync import semantic_events

    old = {
        "version": "0.6.0",
        "release_history": [{"version": "0.6.0", "documented_changes": ["Stable."]}],
    }
    new = {
        **old,
        "capabilities": [],
        "release_history": [{**old["release_history"][0], "source_commit": "a" * 40}],
    }
    assert semantic_events({"sdk-javascript": old}, {"sdk-javascript": new}, "today") == []


def test_release_configuration_mismatch_does_not_attribute_capabilities():
    release = {"version": "0.7.1", "tag": "v0.7.1", "source_commit": "a" * 40}
    notes = [{"version": "0.7.1", "notes": ["add http2 extra"]}]
    assert (
        python_capabilities(
            SDKSource(),
            {},
            "typesafe-ai/typesafe-sdk-python",
            [release],
            {"releases": {"0.7.1": []}},
            notes,
        )
        == []
    )


def test_missing_capability_evidence_retains_unknown_not_deprecated():
    previous = [
        {
            "id": "python-http2-extra",
            "introduced": "0.7.2",
            "status": "documented",
            "provenance": [{"source_id": "known"}],
        }
    ]
    result = python_capabilities(
        SDKSource(), {}, "typesafe-ai/typesafe-sdk-python", [], {}, [], previous
    )
    assert result == [{**previous[0], "status": "unknown"}]


class ReleaseSources(PublicSources):
    """Keep registry, release and development source evidence independently editable."""

    def __init__(self, kind="python"):
        super().__init__()
        self.kind = kind
        self.repo = f"typesafe-ai/typesafe-sdk-{'python' if kind == 'python' else 'js'}"
        self.main_commit = "a" * 40
        self.release_commit = "b" * 40
        self.config_version = "0.1.0"
        self.registry_version = "0.1.0"
        self.tags = [{"name": "v0.1.0", "commit": {"sha": self.release_commit}}]
        self.releases = [{"tag_name": "v0.1.0", "published_at": "2026-09-01"}]
        self.config_body = None
        self.config_status = None
        self.requested = []

    def get(self, url):
        from jev_docs.sync import FetchError, Response

        self.requested.append(url)
        if self.repo in url:
            if "/tags?" in url:
                body = self.tags
            elif "/releases?" in url:
                body = self.releases
            elif "/commits/" in url:
                body = {"sha": self.main_commit}
            elif url.endswith(("pyproject.toml", "package.json")):
                if self.config_status:
                    raise FetchError("release configuration unavailable", self.config_status)
                if self.config_body is not None:
                    body = self.config_body
                elif self.main_commit in url and self.main_commit != self.release_commit:
                    body = (
                        'version = "0.2.0"\nrequires-python = ">=3.14"\n'
                        if self.kind == "python"
                        else {"version": "0.2.0", "engines": {"node": ">=24"}}
                    )
                else:
                    body = (
                        f'version = "{self.config_version}"\nrequires-python = ">=3.10"\n'
                        if self.kind == "python"
                        else {"version": self.config_version, "engines": {"node": ">=18"}}
                    )
            elif url.endswith("docs/changelog.md"):
                body = "## v0.1.0 (2026-09-01)\n\n- add http2 extra\n"
            else:
                return super().get(url)
            return Response(url, (body if isinstance(body, str) else json.dumps(body)).encode(), {})
        if (self.kind == "python" and "pypi.org" in url) or (
            self.kind == "javascript" and "registry.npmjs.org" in url
        ):
            body = (
                {
                    "info": {"version": self.registry_version},
                    "releases": {self.registry_version: []},
                }
                if self.kind == "python"
                else {"dist-tags": {"latest": self.registry_version}}
            )
            return Response(url, json.dumps(body).encode(), {})
        return super().get(url)


def release_snapshot(upstream):
    from jev_docs.sync import sdk_snapshot

    artifacts = {}
    state = sdk_snapshot(
        upstream,
        artifacts,
        upstream.repo,
        "typesafe-sdk" if upstream.kind == "python" else "@typesafe-ai/sdk",
        upstream.kind,
        "pyproject.toml" if upstream.kind == "python" else "package.json",
        "## v0.1.0 (2026-09-01)\n\n- add http2 extra\n",
        None,
    )
    return state, artifacts


@pytest.mark.parametrize("kind", ["python", "javascript"])
@pytest.mark.parametrize("main_ahead", [False, True])
def test_runtime_and_provenance_use_matching_release_config(kind, main_ahead):
    upstream = ReleaseSources(kind)
    if not main_ahead:
        upstream.main_commit = upstream.release_commit
    state, artifacts = release_snapshot(upstream)
    assert state["runtime"] == {kind: ">=3.10" if kind == "python" else ">=18"}
    assert state["source_commit"] == upstream.release_commit
    assert state["main_commit"] == upstream.main_commit
    assert state["discrepancies"] == []
    configs = [a for a in artifacts.values() if a.source_type == "github-source"]
    assert configs and all(upstream.release_commit in a.url for a in configs)
    assert all(upstream.release_commit in p["url"] for p in state["provenance"][1:])
    assert state["public_surface"]["source_class"] == "curated"
    assert state["release_history"][0]["documented_changes_source_class"] == (
        "mutable_documentation"
    )


@pytest.mark.parametrize("kind", ["python", "javascript"])
@pytest.mark.parametrize("problem", ["tag", "release", "version", "missing", "malformed"])
def test_unresolved_release_runtime_stays_unknown_without_main_fallback(kind, problem):
    upstream = ReleaseSources(kind)
    expected = {
        "tag": "registry_version_missing_git_tag",
        "release": "registry_version_missing_github_release",
        "version": "release_config_version_mismatch",
        "missing": "release_config_missing",
        "malformed": "release_config_malformed",
    }[problem]
    if problem == "tag":
        upstream.tags = []
    elif problem == "release":
        upstream.releases = []
    elif problem == "version":
        upstream.config_version = "0.2.0"
    elif problem == "missing":
        upstream.config_status = 404
    else:
        upstream.config_body = "not package configuration"
    state, _ = release_snapshot(upstream)
    assert expected in state["discrepancies"]
    assert state["runtime"] == {kind: "unknown"}
    assert state["source_commit"] is None and state["source_tag"] is None
    assert state["capabilities"] == []
    assert all(upstream.main_commit not in url for url in upstream.requested)
    assert all("raw.githubusercontent.com" not in p["url"] for p in state["provenance"])


@pytest.mark.parametrize(
    "metadata",
    [None, {}, [None], [{"tag_name": "v0.1.0", "published_at": {}}]],
)
def test_malformed_release_metadata_is_explicit_and_runtime_unknown(metadata):
    upstream = ReleaseSources()
    upstream.releases = metadata
    state, _ = release_snapshot(upstream)
    assert "malformed_release_metadata" in state["discrepancies"]
    assert state["runtime"] == {"python": "unknown"}
    assert state["source_commit"] is None
    assert state["capabilities"] == []


def test_registry_publish_delay_keeps_last_published_release_evidence():
    upstream = ReleaseSources()
    upstream.tags.append({"name": "v0.2.0", "commit": {"sha": "c" * 40}})
    upstream.releases.append({"tag_name": "v0.2.0"})
    state, _ = release_snapshot(upstream)
    assert state["version"] == "0.1.0"
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {"python": ">=3.10"}
    assert state["discrepancies"] == ["git_source_ahead_of_registry"]
    assert all(item["introduced"] == "0.1.0" for item in state["capabilities"])
    assert not any("c" * 40 in url for url in upstream.requested)


def test_main_ahead_remains_fresh_after_grace_and_repeated_sync_is_identical(tmp_path, monkeypatch):
    from test_transactions import snapshot

    from jev_docs.sync import synchronize

    upstream = ReleaseSources()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    before = snapshot(tmp_path)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-08T00:00:00Z")
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before
    freshness = json.loads((tmp_path / "state/freshness.json").read_text())
    assert freshness["python"]["status"] == "up_to_date"
    assert "discrepancy_first_observed" not in freshness["python"]


@pytest.mark.parametrize("problem", ["missing_tag", "publish_delay"])
def test_release_discrepancy_retries_are_stable_and_keep_continuous_grace(
    tmp_path, monkeypatch, problem
):
    from test_transactions import snapshot

    from jev_docs.sync import synchronize

    upstream = ReleaseSources()
    if problem == "missing_tag":
        upstream.tags = []
    else:
        upstream.tags.append({"name": "v0.2.0", "commit": {"sha": "c" * 40}})
        upstream.releases.append({"tag_name": "v0.2.0"})
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    before = snapshot(tmp_path)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before
    upstream.pages["another"] = "# Another page"
    synchronize(tmp_path, upstream, strict=True)
    freshness = json.loads((tmp_path / "state/freshness.json").read_text())
    assert freshness["python"]["discrepancy_first_observed"] == "2026-10-01T00:00:00Z"
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-05T00:00:00Z")
    with pytest.raises(RuntimeError, match="beyond grace period: python"):
        synchronize(tmp_path, upstream, strict=True)


def test_transient_release_config_failure_still_fails_sync(tmp_path):
    from jev_docs.sync import synchronize

    upstream = ReleaseSources()
    upstream.config_status = 503
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True)
    assert not (tmp_path / "state").exists()
