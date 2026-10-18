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
            return Response(url, b'[project]\nname = "typesafe-sdk"\nversion = "0.7.2"\n', {})
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
        {"info": {"name": "typesafe-sdk"}, "releases": {"0.7.2": []}},
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
            {"info": {"name": "typesafe-sdk"}, "releases": {"0.7.1": []}},
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
                        '[project]\nname = "typesafe-sdk"\nversion = "0.2.0"\nrequires-python = ">=3.14"\n'
                        if self.kind == "python"
                        else {
                            "name": "@typesafe-ai/sdk",
                            "version": "0.2.0",
                            "engines": {"node": ">=24"},
                        }
                    )
                else:
                    body = (
                        f'[project]\nname = "typesafe-sdk"\nversion = "{self.config_version}"\nrequires-python = ">=3.10"\n'
                        if self.kind == "python"
                        else {
                            "name": "@typesafe-ai/sdk",
                            "version": self.config_version,
                            "engines": {"node": ">=18"},
                        }
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
                    "info": {"name": "typesafe-sdk", "version": self.registry_version},
                    "releases": {self.registry_version: []},
                }
                if self.kind == "python"
                else {"name": "@typesafe-ai/sdk", "dist-tags": {"latest": self.registry_version}}
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
        upstream.config_body = "not package configuration" if kind == "python" else "{}"
    state, _ = release_snapshot(upstream)
    assert expected in state["discrepancies"]
    assert state["runtime"] == {kind: "unknown"}
    assert state["source_commit"] is None and state["source_tag"] is None
    assert state["capabilities"] == []
    assert all(upstream.main_commit not in url for url in upstream.requested)
    assert all("raw.githubusercontent.com" not in p["url"] for p in state["provenance"])


@pytest.mark.parametrize("kind", ["python", "javascript"])
@pytest.mark.parametrize(
    "sha",
    ["main", "refs/heads/main", "../main", "abc", "g" * 40, "a" * 39, "a" * 41],
)
def test_invalid_release_commit_never_attributes_source_evidence(kind, sha):
    upstream = ReleaseSources(kind)
    upstream.tags[0]["commit"]["sha"] = sha
    state, artifacts = release_snapshot(upstream)
    assert "malformed_release_metadata" in state["discrepancies"]
    assert state["malformed_release_metadata"] == ["tag_entry_0_invalid_commit"]
    assert state["source_commit"] is None and state["source_tag"] is None
    assert state["release_history"][0]["source_commit"] is None
    assert state["runtime"] == {kind: "unknown"}
    assert state["capabilities"] == []
    assert not any("raw.githubusercontent.com" in url for url in upstream.requested)
    assert not any(a.source_type == "github-source" for a in artifacts.values())


@pytest.mark.parametrize(
    "metadata",
    [None, {}, [None], [{"tag_name": "v0.1.0", "published_at": {}}]],
)
def test_malformed_release_metadata_is_explicit_and_runtime_unknown(metadata):
    from jev_docs.sync import FetchError, release_provenance

    upstream = ReleaseSources()
    upstream.releases = metadata
    info = release_provenance(upstream.registry_version, upstream.tags, metadata)
    assert "malformed_release_metadata" in info["discrepancies"]
    assert info["source_commit"] is None
    with pytest.raises(FetchError) as caught:
        release_snapshot(upstream)
    assert caught.value.code == "invalid_schema"


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


@pytest.mark.parametrize("main_ahead", [False, True])
def test_legacy_release_evidence_correction_is_tracking_baseline_once(
    tmp_path, monkeypatch, main_ahead
):
    from test_transactions import snapshot

    from jev_docs.sync import canonical_json, observed_state, synchronize

    upstream = ReleaseSources()
    if not main_ahead:
        upstream.main_commit = upstream.release_commit
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    sdk_path = tmp_path / "state/sdk-python.json"
    legacy = json.loads(sdk_path.read_text())
    assert legacy.pop("release_evidence_policy") == "tag-verified-v1"
    legacy.pop("fingerprint")
    legacy["public_surface"].pop("source_class")
    legacy["public_surface"].pop("scope")
    if main_ahead:
        legacy["runtime"] = {"python": ">=3.14"}
        legacy["provenance"][1]["url"] = legacy["provenance"][1]["url"].replace(
            upstream.release_commit, upstream.main_commit
        )
    sdk_path.write_text(canonical_json(observed_state(legacy, None)) + "\n")
    historical_baseline = (tmp_path / "events/baseline.json").read_bytes()
    requested = set(upstream.requested)

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    result = synchronize(tmp_path, upstream, strict=True)
    assert set(upstream.requested) == requested
    assert result["events"] == 1
    events = json.loads((tmp_path / f"events/{result['day']}.json").read_text())["events"]
    assert len(events) == 1
    event = events[0]
    assert event["entity"] == "sdk-python" and event["change"] == "baseline"
    assert "local correction" in event["summary"]
    assert "not an upstream release change" in event["summary"]
    corrected = json.loads(sdk_path.read_text())
    assert corrected["release_evidence_policy"] == "tag-verified-v1"
    assert corrected["runtime"] == {"python": ">=3.10"}
    assert (tmp_path / "events/baseline.json").read_bytes() == historical_baseline

    before = snapshot(tmp_path)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_release_provenance_labels_do_not_create_semantic_behavior_events(kind):
    import copy

    from jev_docs.sync import semantic_events

    upstream = ReleaseSources(kind)
    upstream.main_commit = upstream.release_commit
    current, _ = release_snapshot(upstream)
    legacy_labels = copy.deepcopy(current)
    legacy_labels["public_surface"].pop("source_class")
    legacy_labels["public_surface"].pop("scope")
    for release in legacy_labels["release_history"]:
        release.pop("documented_changes_source_class", None)
    key = f"sdk-{kind}"
    assert semantic_events({key: legacy_labels}, {key: current}, "today") == []
    changed = copy.deepcopy(current)
    changed["runtime"][kind] = ">=999"
    events = semantic_events({key: current}, {key: changed}, "today")
    assert len(events) == 1 and events[0]["change"] == "modified"


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_stable_release_ignores_prerelease_and_draft_metadata(kind):
    upstream = ReleaseSources(kind)
    for tag, extra in [
        ("v0.8.0-rc.1", {}),
        ("v0.8.0-beta.2", {"prerelease": True}),
        ("v0.8.0", {"draft": True}),
    ]:
        upstream.tags.append({"name": tag, "commit": {"sha": "c" * 40}})
        upstream.releases.append({"tag_name": tag, **extra})
    # Draft tags are not normally public; do not manufacture one for this fixture.
    upstream.tags.pop()
    state, _ = release_snapshot(upstream)
    assert state["discrepancies"] == []
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {kind: ">=3.10" if kind == "python" else ">=18"}
    assert [item["tag"] for item in state["release_history"]] == ["v0.1.0"]


def test_policy_upgrade_preserves_concurrent_release_event_once(tmp_path, monkeypatch):
    from test_transactions import snapshot

    from jev_docs.sync import canonical_json, observed_state, synchronize

    upstream = ReleaseSources()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    sdk_path = tmp_path / "state/sdk-python.json"
    legacy = json.loads(sdk_path.read_text())
    legacy.pop("release_evidence_policy")
    legacy.pop("fingerprint")
    sdk_path.write_text(canonical_json(observed_state(legacy, None)) + "\n")
    baseline = (tmp_path / "events/baseline.json").read_bytes()
    upstream.registry_version = upstream.config_version = "0.2.0"
    upstream.release_commit = "c" * 40
    upstream.tags.append({"name": "v0.2.0", "commit": {"sha": upstream.release_commit}})
    upstream.releases.append({"tag_name": "v0.2.0", "published_at": "2026-10-02"})
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    result = synchronize(tmp_path, upstream, strict=True)
    events = json.loads((tmp_path / f"events/{result['day']}.json").read_text())["events"]
    sdk_events = [event for event in events if event["entity"] == "sdk-python"]
    assert len(sdk_events) == 1 and sdk_events[0]["change"] == "modified"
    assert (tmp_path / "events/baseline.json").read_bytes() == baseline
    before = snapshot(tmp_path)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("field,value", [("source_tag", "v0.1.1"), ("source_commit", "c" * 40)])
def test_policy_upgrade_does_not_hide_changed_release_identity(field, value):
    import copy

    from jev_docs.sync import semantic_events

    current, _ = release_snapshot(ReleaseSources())
    legacy = copy.deepcopy(current)
    legacy.pop("release_evidence_policy")
    current[field] = value
    events = semantic_events({"sdk-python": legacy}, {"sdk-python": current}, "today")
    assert len(events) == 1 and events[0]["change"] == "modified"


@pytest.mark.parametrize(
    "record",
    [
        {"tag_name": "v0.8.0-rc.1", "published_at": {}},
        {"tag_name": "nightly", "prerelease": "true"},
        {"draft": True},
        {"prerelease": True},
    ],
)
def test_excluded_release_metadata_still_rejects_malformed_records(record):
    from jev_docs.sync import FetchError, release_provenance

    upstream = ReleaseSources()
    upstream.releases.append(record)
    info = release_provenance(upstream.registry_version, upstream.tags, upstream.releases)
    assert "malformed_release_metadata" in info["discrepancies"]
    assert info["source_commit"] is None
    with pytest.raises(FetchError) as caught:
        release_snapshot(upstream)
    assert caught.value.code == "invalid_schema"


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_stable_looking_excluded_release_tag_does_not_create_publish_delay(flag):
    upstream = ReleaseSources()
    upstream.tags.append({"name": "v0.2.0", "commit": {"sha": "c" * 40}})
    upstream.releases.append({"tag_name": "v0.2.0", flag: True})
    state, _ = release_snapshot(upstream)
    assert state["discrepancies"] == []
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {"python": ">=3.10"}
    assert "v0.2.0" in state["available_tags"]


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_duplicate_release_status_classifications_fail_closed(flag):
    upstream = ReleaseSources()
    upstream.releases.append({"tag_name": "v0.1.0", flag: True})
    state, _ = release_snapshot(upstream)
    assert "malformed_release_metadata" in state["discrepancies"]
    assert "release_entry_1_conflicting_status" in state["malformed_release_metadata"]
    assert state["source_commit"] is None
    assert state["runtime"] == {"python": "unknown"}


@pytest.mark.parametrize("kind", ["python", "javascript"])
@pytest.mark.parametrize("missing", ["tag", "release"])
def test_legacy_unresolved_release_resolution_is_modified_once(
    tmp_path, monkeypatch, kind, missing
):
    from test_transactions import snapshot

    from jev_docs.sync import canonical_json, observed_state, synchronize

    upstream = ReleaseSources(kind)
    tags, releases = upstream.tags, upstream.releases
    if missing == "tag":
        upstream.tags = []
    else:
        upstream.releases = []
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    sdk_path = tmp_path / f"state/sdk-{kind}.json"
    legacy = json.loads(sdk_path.read_text())
    assert legacy["source_tag"] is None and legacy["source_commit"] is None
    assert legacy["discrepancies"]
    legacy.pop("release_evidence_policy")
    legacy.pop("fingerprint")
    sdk_path.write_text(canonical_json(observed_state(legacy, None)) + "\n")
    baseline = (tmp_path / "events/baseline.json").read_bytes()

    upstream.tags, upstream.releases = tags, releases
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    result = synchronize(tmp_path, upstream, strict=True)
    events = json.loads((tmp_path / f"events/{result['day']}.json").read_text())["events"]
    sdk_events = [event for event in events if event["entity"] == f"sdk-{kind}"]
    assert len(sdk_events) == 1 and sdk_events[0]["change"] == "modified"
    assert "not an upstream release change" not in sdk_events[0]["summary"]
    resolved = json.loads(sdk_path.read_text())
    assert resolved["source_commit"] == upstream.release_commit
    assert resolved["discrepancies"] == []
    assert (tmp_path / "events/baseline.json").read_bytes() == baseline
    before = snapshot(tmp_path)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("field", ["source_tag", "source_commit"])
@pytest.mark.parametrize("resolving", [False, True])
def test_policy_upgrade_preserves_one_sided_release_identity_changes(field, resolving):
    import copy

    from jev_docs.sync import semantic_events

    current, _ = release_snapshot(ReleaseSources())
    legacy = copy.deepcopy(current)
    legacy.pop("release_evidence_policy")
    (legacy if resolving else current)[field] = None
    events = semantic_events({"sdk-python": legacy}, {"sdk-python": current}, "today")
    assert len(events) == 1 and events[0]["change"] == "modified"


@pytest.mark.parametrize("resolving", [False, True])
def test_policy_upgrade_preserves_discrepancy_changes_without_identity_change(resolving):
    import copy

    from jev_docs.sync import semantic_events

    current, _ = release_snapshot(ReleaseSources())
    legacy = copy.deepcopy(current)
    legacy.pop("release_evidence_policy")
    (legacy if resolving else current)["discrepancies"] = ["git_source_ahead_of_registry"]
    events = semantic_events({"sdk-python": legacy}, {"sdk-python": current}, "today")
    assert len(events) == 1 and events[0]["change"] == "modified"


@pytest.mark.parametrize("kind", ["python", "javascript"])
@pytest.mark.parametrize(
    "tag,ahead",
    [
        ("v0.8.0+build.1", True),
        ("v0.7.5+build.1", False),
        ("v0.8.0-rc.1+build.1", False),
        ("v0.8.0-01", False),
        ("nightly-2026-10-01", False),
    ],
)
def test_semver_and_unfamiliar_tags_preserve_matching_stable_release(kind, tag, ahead):
    upstream = ReleaseSources(kind)
    upstream.registry_version = upstream.config_version = "0.7.5"
    upstream.tags = [{"name": "v0.7.5", "commit": {"sha": upstream.release_commit}}]
    upstream.releases = [{"tag_name": "v0.7.5"}]
    upstream.tags.append({"name": tag, "commit": {"sha": "c" * 40}})
    upstream.releases.append({"tag_name": tag})
    state, artifacts = release_snapshot(upstream)
    assert state["discrepancies"] == (["git_source_ahead_of_registry"] if ahead else [])
    assert state["source_tag"] == "v0.7.5"
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {kind: ">=3.10" if kind == "python" else ">=18"}
    assert tag in state["available_tags"]
    assert not any("c" * 40 in url for url in upstream.requested)
    assert all(
        artifact.upstream_commit == upstream.release_commit
        for artifact in artifacts.values()
        if artifact.source_type == "github-source"
    )


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_current_stable_build_tag_uses_its_exact_verified_config(kind):
    upstream = ReleaseSources(kind)
    upstream.registry_version = upstream.config_version = "0.7.5+build.1"
    upstream.tags = [{"name": "v0.7.5+build.1", "commit": {"sha": upstream.release_commit}}]
    upstream.releases = [{"tag_name": "v0.7.5+build.1"}]
    state, _ = release_snapshot(upstream)
    assert state["discrepancies"] == []
    assert state["source_tag"] == "v0.7.5+build.1"
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {kind: ">=3.10" if kind == "python" else ">=18"}


def test_prerelease_build_registry_version_does_not_claim_stable_release():
    upstream = ReleaseSources()
    upstream.registry_version = upstream.config_version = "0.8.0-rc.1+build.1"
    upstream.tags.append({"name": "v0.8.0-rc.1+build.1", "commit": {"sha": "c" * 40}})
    upstream.releases.append({"tag_name": "v0.8.0-rc.1+build.1"})
    state, _ = release_snapshot(upstream)
    assert state["discrepancies"] == ["registry_version_unresolved"]
    assert state["source_tag"] is None and state["source_commit"] is None
    assert state["runtime"] == {"python": "unknown"}


@pytest.mark.parametrize("tag,ahead", [("v0.8.0+build.1", True), ("v0.8.0-rc.1+build.1", False)])
def test_build_release_pagination_keeps_capabilities_pinned_to_current_release(tag, ahead):
    from jev_docs.sync import Response

    class PaginatedReleaseSources(ReleaseSources):
        def get(self, url):
            if self.repo in url and ("/tags?" in url or "/releases?" in url):
                self.requested.append(url)
                tags = "/tags?" in url
                if url.endswith("page=2"):
                    body = self.tags if tags else self.releases
                    headers = {}
                else:
                    body = (
                        [{"name": tag, "commit": {"sha": "c" * 40}}]
                        if tags
                        else [{"tag_name": tag}]
                    )
                    endpoint = "tags" if tags else "releases"
                    next_url = f"https://api.github.com/repos/{self.repo}/{endpoint}?page=2"
                    headers = {"Link": f'<{next_url}>; rel="next"'}
                return Response(url, json.dumps(body).encode(), headers)
            return super().get(url)

    upstream = PaginatedReleaseSources()
    state, _ = release_snapshot(upstream)
    assert len([url for url in upstream.requested if url.endswith("page=2")]) == 2
    assert state["discrepancies"] == (["git_source_ahead_of_registry"] if ahead else [])
    assert state["capabilities"]
    for capability in state["capabilities"]:
        assert capability["introduced"] == "0.1.0"
        assert capability["source_tag"] == "v0.1.0"
        assert capability["source_commit"] == upstream.release_commit
        assert all(upstream.release_commit in p["url"] for p in capability["provenance"])
    assert not any("c" * 40 in url for url in upstream.requested)


@pytest.mark.parametrize(
    "heading", ["## v{version} (2026-10-01)", "<h2>v{version} (2026-10-01)</h2>"]
)
def test_build_version_capabilities_require_exact_immutable_release_notes(heading):
    from jev_docs.sync import Response, sdk_snapshot

    version = "0.7.5+build.1"
    notes = heading.format(version=version) + "\n\n- add http2 extra\n"

    class BuildReleaseSources(ReleaseSources):
        def get(self, url):
            if self.repo in url and url.endswith("docs/changelog.md"):
                self.requested.append(url)
                return Response(url, notes.encode(), {})
            return super().get(url)

    upstream = BuildReleaseSources()
    upstream.registry_version = upstream.config_version = version
    upstream.tags = [{"name": f"v{version}", "commit": {"sha": upstream.release_commit}}]
    upstream.releases = [{"tag_name": f"v{version}"}]
    artifacts = {}
    state = sdk_snapshot(
        upstream, artifacts, upstream.repo, "typesafe-sdk", "python", "pyproject.toml", notes, None
    )
    assert state["discrepancies"] == []
    assert state["release_history"][0]["documented_changes"] == ["add http2 extra"]
    assert len(state["capabilities"]) == 1
    capability = state["capabilities"][0]
    assert capability["id"] == "python-http2-extra"
    assert capability["introduced"] == version
    assert capability["source_tag"] == f"v{version}"
    assert capability["source_commit"] == upstream.release_commit
    assert capability["status"] == "documented"
    assert len(capability["provenance"]) == 2
    for evidence in capability["provenance"]:
        assert upstream.release_commit in evidence["url"]
        assert evidence["sha256"] == artifacts[evidence["source_id"]].sha256
    assert all(
        upstream.main_commit not in url
        for url in upstream.requested
        if "raw.githubusercontent.com" in url
    )

    # A similarly named immutable release cannot supply this exact build's notes.
    notes = heading.format(version="0.7.5+build.2") + "\n\n- add http2 extra\n"
    result = python_capabilities(
        upstream,
        {},
        upstream.repo,
        state["release_history"],
        {"info": {"name": "typesafe-sdk"}, "releases": {version: []}},
        [{"version": version, "notes": ["add http2 extra"]}],
        state["capabilities"],
    )
    assert result == [{**capability, "status": "unknown"}]


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_policy_upgrade_preserves_older_release_backfill_once(tmp_path, monkeypatch, kind):
    from test_transactions import snapshot

    from jev_docs.sync import canonical_json, observed_state, synchronize

    upstream = ReleaseSources(kind)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    sdk_path = tmp_path / f"state/sdk-{kind}.json"
    legacy = json.loads(sdk_path.read_text())
    legacy.pop("release_evidence_policy")
    legacy.pop("fingerprint")
    sdk_path.write_text(canonical_json(observed_state(legacy, None)) + "\n")
    baseline = (tmp_path / "events/baseline.json").read_bytes()
    upstream.tags.append({"name": "v0.0.9", "commit": {"sha": "c" * 40}})
    upstream.releases.append({"tag_name": "v0.0.9", "published_at": "2026-08-31"})

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    result = synchronize(tmp_path, upstream, strict=True)
    events = json.loads((tmp_path / f"events/{result['day']}.json").read_text())["events"]
    sdk_events = [event for event in events if event["entity"] == f"sdk-{kind}"]
    assert len(sdk_events) == 1 and sdk_events[0]["change"] == "modified"
    assert "not an upstream release change" not in sdk_events[0]["summary"]
    current = json.loads(sdk_path.read_text())
    for field in ("version", "source_tag", "source_commit", "discrepancies"):
        assert current[field] == legacy[field]
    assert [release["version"] for release in current["release_history"]] == ["0.0.9", "0.1.0"]
    assert (tmp_path / "events/baseline.json").read_bytes() == baseline
    before = snapshot(tmp_path)
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "field,value", [("name", "Corrected title"), ("published_at", "2026-09-02")]
)
def test_policy_upgrade_preserves_changed_release_metadata(field, value):
    import copy

    from jev_docs.sync import semantic_events

    current, _ = release_snapshot(ReleaseSources())
    legacy = copy.deepcopy(current)
    legacy.pop("release_evidence_policy")
    current["release_history"][0][field] = value
    events = semantic_events({"sdk-python": legacy}, {"sdk-python": current}, "today")
    assert len(events) == 1 and events[0]["change"] == "modified"


def test_policy_upgrade_release_history_source_enrichment_is_still_local_baseline():
    import copy

    from jev_docs.sync import semantic_events

    current, _ = release_snapshot(ReleaseSources())
    legacy = copy.deepcopy(current)
    legacy.pop("release_evidence_policy")
    for release in legacy["release_history"]:
        release.pop("source_commit")
        release.pop("documented_changes_source_class", None)
    events = semantic_events({"sdk-python": legacy}, {"sdk-python": current}, "today")
    assert len(events) == 1 and events[0]["change"] == "baseline"


@pytest.mark.parametrize(
    "config,version,runtime",
    [
        (
            '[tool.example]\nversion = "0.1.0"\nrequires-python = ">=9"\n'
            '[project]\nname = "typesafe-sdk"\nversion = "0.2.0"\nrequires-python = ">=3.10"\n',
            "0.2.0",
            ">=3.10",
        ),
        (
            'description = """\nversion = "0.1.0"\nrequires-python = ">=9"\n"""\n'
            '[project]\nname = "typesafe-sdk"\nversion = "0.2.0"\nrequires-python = ">=3.10"\n',
            "0.2.0",
            ">=3.10",
        ),
        (
            '["project"]\n"name" = "typesafe-sdk"\n"version" = "0.1.0"\n\'requires-python\' = ">=3.10"\n',
            "0.1.0",
            ">=3.10",
        ),
        (
            '[project]\nversion = "0.1.0"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n',
            None,
            None,
        ),
        (
            '[project]\nversion = "0.1.0"\ndynamic = ["version"]\nrequires-python = ">=3.10"\n',
            None,
            ">=3.10",
        ),
        (
            '[project]\ndynamic = ["version"]\n[tool.example]\nversion = "0.1.0"\n',
            None,
            None,
        ),
        ('version = "0.1.0"\nrequires-python = ">=3.10"\n', None, None),
        ('[tool.example]\nversion = "0.1.0"\nrequires-python = ">=3.10"\n', None, None),
        ('[project]\nversion = 1\nrequires-python = ["3.10"]\n', None, None),
        ('[project]\nversion = "0.1.0"\ndynamic = "version"\n', None, None),
    ],
    ids=[
        "wrong-table",
        "misleading-multiline-string",
        "quoted-project-keys",
        "duplicate-version",
        "dynamic-static-version-conflict",
        "dynamic-version-no-tool-fallback",
        "no-root-fallback",
        "no-tool-fallback",
        "non-string-fields",
        "malformed-dynamic",
    ],
)
def test_python_release_configuration_uses_only_static_project_fields(config, version, runtime):
    from jev_docs.sync import parse_simple_toml_value

    assert parse_simple_toml_value(config, "version") == version
    assert parse_simple_toml_value(config, "requires-python") == runtime
    upstream = ReleaseSources()
    upstream.config_body = config
    state, _ = release_snapshot(upstream)
    if version == upstream.registry_version:
        assert state["discrepancies"] == []
        assert state["source_commit"] == upstream.release_commit
        assert state["runtime"] == {"python": runtime}
        assert state["capabilities"]
        assert all(
            item["source_commit"] == upstream.release_commit for item in state["capabilities"]
        )
    else:
        discrepancy = (
            "release_config_malformed" if version is None else "release_config_version_mismatch"
        )
        assert state["discrepancies"] == [discrepancy]
        assert state["source_commit"] is None and state["source_tag"] is None
        assert state["runtime"] == {"python": "unknown"}
        assert state["capabilities"] == []


def test_python_dynamic_runtime_is_unknown_without_tool_table_fallback():
    upstream = ReleaseSources()
    upstream.config_body = (
        '[project]\nname = "typesafe-sdk"\nversion = "0.1.0"\ndynamic = ["requires-python"]\n'
        '[tool.example]\nrequires-python = ">=9"\n'
    )
    state, _ = release_snapshot(upstream)
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {"python": "unknown"}
    assert state["discrepancies"] == []


@pytest.mark.parametrize(
    "config",
    [
        '[tool.example]\nversion = "0.1.0"\n[project]\nversion = "0.2.0"\n',
        'description = """\nversion = "0.1.0"\n"""\n[project]\nversion = "0.2.0"\n',
        '[project]\nversion = "0.1.0"\ndynamic = ["version"]\n',
    ],
)
def test_evaluation_tool_cannot_verify_unrelated_or_dynamic_project_version(config):
    from jev_docs.sync import Response, evaluation_tool_snapshot

    class EvaluationConfigSources(PublicSources):
        def get(self, url):
            if "system-one-adapter-python" in url and url.endswith("pyproject.toml"):
                return Response(url, config.encode(), {})
            return super().get(url)

    with pytest.raises(RuntimeError, match="evaluation tool release/config mismatch"):
        evaluation_tool_snapshot(EvaluationConfigSources(), {}, None)


@pytest.mark.parametrize("kind", ["python", "javascript"])
@pytest.mark.parametrize("name", [None, "unrelated-package"])
def test_sdk_release_requires_matching_config_package_name(kind, name):
    upstream = ReleaseSources(kind)
    if kind == "python":
        upstream.config_body = '[project]\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        if name is not None:
            upstream.config_body += f'name = "{name}"\n'
    else:
        upstream.config_body = {"version": "0.1.0", "engines": {"node": ">=18"}}
        if name is not None:
            upstream.config_body["name"] = name
    state, _ = release_snapshot(upstream)
    discrepancy = "release_config_name_missing" if name is None else "release_config_name_mismatch"
    assert state["discrepancies"] == [discrepancy]
    assert state["source_tag"] is None and state["source_commit"] is None
    assert state["runtime"] == {kind: "unknown"}
    assert state["capabilities"] == []
    assert len(state["provenance"]) == 1


@pytest.mark.parametrize("observed_name", ["TypeSafe_SDK", "TYPEsafe...___SDK"])
def test_python_package_name_normalization_preserves_observed_registry_evidence(observed_name):
    from jev_docs.sync import Response

    class NormalizedNames(ReleaseSources):
        def get(self, url):
            response = super().get(url)
            if "pypi.org" in url:
                body = json.loads(response.body)
                body["info"]["name"] = observed_name
                return Response(url, json.dumps(body).encode(), {})
            return response

    upstream = NormalizedNames()
    upstream.config_body = (
        '[project]\nname = "typesafe.sdk"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
    )
    state, artifacts = release_snapshot(upstream)
    assert state["package"] == "typesafe-sdk"
    assert state["source_commit"] == upstream.release_commit
    assert state["runtime"] == {"python": ">=3.10"}
    assert state["discrepancies"] == []
    assert state["capabilities"]
    registry = artifacts["registry:pypi:typesafe-sdk"]
    assert json.loads(registry.content)["name"] == observed_name
    canonical, canonical_artifacts = release_snapshot(ReleaseSources())
    assert registry.sha256 != canonical_artifacts["registry:pypi:typesafe-sdk"].sha256
    assert canonical["package"] == state["package"]


@pytest.mark.parametrize("name", [None, "@Typesafe-ai/sdk", "@typesafe-ai/other"])
def test_npm_scoped_package_name_matching_is_exact(name):
    upstream = ReleaseSources("javascript")
    upstream.config_body = {"version": "0.1.0", "engines": {"node": ">=18"}}
    if name is not None:
        upstream.config_body["name"] = name
    state, _ = release_snapshot(upstream)
    assert state["source_commit"] is None
    assert state["runtime"] == {"javascript": "unknown"}
    expected = "release_config_name_missing" if name is None else "release_config_name_mismatch"
    assert state["discrepancies"] == [expected]


@pytest.mark.parametrize("name", [None, "unrelated-package"])
def test_immutable_capabilities_require_the_registry_project_name(name):
    from jev_docs.sync import Response

    release = {"version": "0.7.2", "tag": "v0.7.2", "source_commit": "a" * 40}
    registry = {"info": {"name": "typesafe-sdk"}, "releases": {"0.7.2": []}}
    notes = [{"version": "0.7.2", "notes": ["add http2 extra"]}]
    previous = python_capabilities(
        SDKSource(), {}, "typesafe-ai/typesafe-sdk-python", [release], registry, notes
    )
    assert previous and previous[0]["status"] == "documented"

    class OtherProject(SDKSource):
        def get(self, url):
            if url.endswith("pyproject.toml"):
                config = '[project]\nversion = "0.7.2"\n'
                if name is not None:
                    config += f'name = "{name}"\n'
                return Response(url, config.encode(), {})
            return super().get(url)

    assert python_capabilities(
        OtherProject(), {}, "typesafe-ai/typesafe-sdk-python", [release], registry, notes, previous
    ) == [{**item, "status": "unknown"} for item in previous]


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_registry_evidence_includes_the_observed_package_name(kind):
    from jev_docs.sync import sha256_json

    state, artifacts = release_snapshot(ReleaseSources(kind))
    registry = next(a for a in artifacts.values() if a.source_type == "package-registry")
    evidence = json.loads(registry.content)
    expected = (
        {"name": "typesafe-sdk", "version": "0.1.0", "published_versions": ["0.1.0"]}
        if kind == "python"
        else {"name": "@typesafe-ai/sdk", "latest": "0.1.0"}
    )
    assert evidence == expected
    assert registry.sha256 == sha256_json(expected) == state["provenance"][0]["sha256"]
    assert registry.sha256 != sha256_json({k: v for k, v in expected.items() if k != "name"})
