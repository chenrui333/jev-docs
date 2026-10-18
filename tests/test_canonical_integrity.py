import json

import pytest
from test_transactions import PublicSources, snapshot

from jev_docs.sync import (
    FetchError,
    Response,
    metadata_json,
    read_json,
    sha256_json,
    synchronize,
    validate_coverage,
    validate_tree,
    write_json,
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, PublicSources(), strict=True)
    return tmp_path


@pytest.mark.parametrize("name", ["sdk-python", "api", "models", "primitives", "freshness"])
def test_semantic_tampering_invalidates_stored_fingerprint(tree, name):
    path = tree / f"state/{name}.json"
    value = json.loads(path.read_text())
    value["runtime" if name == "sdk-python" else "schema_version"] = "tampered"
    write_json(path, value)
    with pytest.raises(RuntimeError, match="fingerprint"):
        validate_tree(tree, strict=True)


@pytest.mark.parametrize("surface", ["page", "discovery", "family", "missing-page"])
def test_coverage_and_family_records_agree_with_sources(tree, surface):
    relative = "state/source-coverage.json"
    value = json.loads((tree / relative).read_text())
    if surface == "family":
        relative = "sources/docs.typesafe.ai/manifest.json"
        value = json.loads((tree / relative).read_text())
        value["artifacts"][0]["sha256"] = "0" * 64
    elif surface == "missing-page":
        relative = "state/sources.json"
        value = json.loads((tree / relative).read_text())
        value["artifacts"] = [r for r in value["artifacts"] if r["source_id"] != "docs:page"]
    else:
        target = value["pages"][0] if surface == "page" else value["discovery"]
        target["sha256"] = "0" * 64
    write_json(tree / relative, value)
    with pytest.raises(RuntimeError, match="coverage|manifest"):
        validate_tree(tree)


@pytest.mark.parametrize(
    "relative",
    [
        "state/sources.json",
        "sources/docs.typesafe.ai/manifest.json",
        "sources/github.typesafe-ai/manifest.json",
    ],
)
def test_duplicate_sources_are_rejected_before_mapping(tree, relative):
    value = json.loads((tree / relative).read_text())
    value["artifacts"].append(value["artifacts"][0])
    write_json(tree / relative, value)
    with pytest.raises(RuntimeError, match="duplicate source"):
        validate_tree(tree)


@pytest.mark.parametrize(
    "relative",
    [
        "state/api.json",
        "state/models.json",
        "state/primitives.json",
        "MODEL_LIMITATIONS.md",
        "sources/docs.typesafe.ai/manifest.json",
        "sources/github.typesafe-ai/manifest.json",
    ],
)
def test_coverage_two_requires_complete_generated_tree(tree, relative):
    (tree / relative).unlink()
    with pytest.raises(RuntimeError, match="missing"):
        validate_tree(tree)


def test_legacy_coverage_and_observation_metadata_remain_accepted(tree):
    coverage = json.loads((tree / "state/source-coverage.json").read_text())
    coverage["schema_version"] = 1
    write_json(tree / "state/source-coverage.json", coverage)
    (tree / "state/api.json").unlink()
    family = tree / "sources/docs.typesafe.ai/manifest.json"
    value = json.loads(family.read_text())
    value["artifacts"][0]["observed_at"] = "2026-10-01T00:00:00.123456Z"
    write_json(family, value)
    validate_tree(tree, strict=True)


def test_freshness_footers_and_top_observation_are_not_hashed(tree):
    path = tree / "state/freshness.json"
    value = json.loads(path.read_text())
    value.update(
        observed_at="2026-10-02T00:00:00Z",
        last_changed="2026-10-02",
        last_successful_observation="2026-10-02T00:00:00Z",
    )
    write_json(path, value)
    validate_tree(tree, strict=True)


def test_honest_repeat_is_byte_identical(tree):
    before = snapshot(tree)
    synchronize(tree, PublicSources(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    "body",
    [
        b'{"private": secret}',
        b"\xff",
        b"[" * 10000 + b"0" + b"]" * 10000,
    ],
    ids=["malformed", "non-utf8", "nested"],
)
def test_corrupt_persisted_json_is_sanitized(tree, body):
    (tree / "state/api.json").write_bytes(body)
    with pytest.raises(RuntimeError) as caught:
        validate_tree(tree)
    message = str(caught.value)
    assert "state/api.json" in message
    assert "read" in message.lower()
    assert str(tree) not in message
    assert "private" not in message
    assert caught.value.__suppress_context__


def test_excessively_nested_upstream_json_is_structured_invalid_json():
    depth = 10000
    response = Response(
        "https://api.github.com/repos/typesafe-ai/example", b"[" * depth + b"0" + b"]" * depth, {}
    )
    with pytest.raises(FetchError) as caught:
        metadata_json(response, "GitHub")
    assert caught.value.code == "invalid_json"


def test_same_origin_redirect_preserves_requested_navigation_identity(tmp_path, monkeypatch):
    class RedirectedSources(PublicSources):
        def get(self, url):
            response = super().get(url)
            if url == "https://docs.typesafe.ai/page.md":
                return Response("https://docs.typesafe.ai/moved.md", response.body, {})
            return response

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    upstream = RedirectedSources()
    synchronize(tmp_path, upstream, strict=True)
    coverage = json.loads((tmp_path / "state/source-coverage.json").read_text())
    assert coverage["pages"][0]["source_id"] == "docs:page"
    assert coverage["pages"][0]["canonical_url"] == "https://docs.typesafe.ai/page"
    sources = json.loads((tmp_path / "state/sources.json").read_text())
    page = next(record for record in sources["artifacts"] if record["source_id"] == "docs:page")
    assert page["fetch_url"] == "https://docs.typesafe.ai/moved.md"
    assert page["canonical_url"] == "https://docs.typesafe.ai/moved"
    validate_tree(tmp_path, strict=True)
    before = snapshot(tmp_path)
    synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "updates",
    [
        {"fetch_url": "https://elsewhere.example/page.md"},
        {"fetch_url": "http://docs.typesafe.ai/page.md"},
        {"canonical_url": "https://docs.typesafe.ai/unrelated"},
    ],
)
def test_resolved_documentation_location_must_be_consistent_and_public(tree, updates):
    for relative in ("state/sources.json", "sources/docs.typesafe.ai/manifest.json"):
        value = json.loads((tree / relative).read_text())
        page = next(record for record in value["artifacts"] if record["source_id"] == "docs:page")
        page.update(updates)
        write_json(tree / relative, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("untrusted documentation must fail before fetching")

    with pytest.raises(RuntimeError):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    "name",
    [
        "api",
        "models",
        "primitives",
        "sdk-python",
        "sdk-javascript",
        "skill",
        "practices",
        "model-limitations",
        "cookbooks",
        "evaluation-tool",
        "freshness",
    ],
)
def test_semantic_state_cannot_bypass_integrity_by_removing_fingerprint(tree, name):
    path = tree / f"state/{name}.json"
    value = json.loads(path.read_text())
    value.pop("fingerprint")
    if name == "api":
        value["endpoint"] = "POST https://api.typesafe.ai/tampered"
    write_json(path, value)
    with pytest.raises(RuntimeError, match="missing stored fingerprint"):
        validate_tree(tree, strict=True)


@pytest.mark.parametrize("depth", [63, 64, 65])
@pytest.mark.parametrize("container", ["array", "object"])
def test_json_container_nesting_bound_ignores_string_brackets_and_escapes(
    tmp_path, depth, container
):
    text = 'Brackets [ { ] }, quote ", backslash \\, escaped quote \\". ' * 100
    opening, closing = ("[", "]") if container == "array" else ('{"value":', "}")
    serialized = opening * depth + json.dumps(text) + closing * depth
    path = tmp_path / "state/input.json"
    path.parent.mkdir()
    path.write_text(serialized, encoding="utf-8")
    response = Response("https://api.github.com/repos/typesafe-ai/example", serialized.encode(), {})
    if depth <= 64:
        expected = json.loads(serialized)
        assert read_json(path) == expected
        assert metadata_json(response, "GitHub") == expected
    else:
        with pytest.raises(RuntimeError, match="cannot read persisted JSON: state/input.json"):
            read_json(path)
        with pytest.raises(FetchError) as caught:
            metadata_json(response, "GitHub")
        assert caught.value.code == "invalid_json"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_bounded_upstream_json_preserves_supported_byte_encodings(encoding):
    response = Response(
        "https://api.github.com/repos/typesafe-ai/example",
        json.dumps({"text": 'Unicode \u2603, brackets []{}, quote ", backslash \\'}).encode(
            encoding
        ),
        {},
    )
    assert metadata_json(response, "GitHub") == {
        "text": 'Unicode \u2603, brackets []{}, quote ", backslash \\'
    }


@pytest.mark.parametrize("error", [ValueError, RecursionError])
def test_decoder_failures_keep_existing_sanitized_error_contract(tmp_path, monkeypatch, error):
    path = tmp_path / "state/input.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")

    def fail_decode(_text):
        raise error("private decoder details")

    monkeypatch.setattr(json, "loads", fail_decode)
    with pytest.raises(RuntimeError, match="cannot read persisted JSON: state/input.json") as local:
        read_json(path)
    assert "private" not in str(local.value)
    assert local.value.__suppress_context__
    with pytest.raises(FetchError) as remote:
        metadata_json(
            Response("https://api.github.com/repos/typesafe-ai/example", b"{}", {}), "GitHub"
        )
    assert remote.value.code == "invalid_json"
    assert "private" not in str(remote.value)
    assert remote.value.__suppress_context__


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("empty_family", ["both", "docs.typesafe.ai", "github.typesafe-ai"])
def test_empty_present_manifests_abort_sync_before_fetch_or_history(tree, schema, empty_family):
    coverage_path = tree / "state/source-coverage.json"
    coverage = json.loads(coverage_path.read_text())
    coverage["schema_version"] = schema
    write_json(coverage_path, coverage)
    for family in ("docs.typesafe.ai", "github.typesafe-ai"):
        if empty_family in {"both", family}:
            path = tree / f"sources/{family}/manifest.json"
            manifest = json.loads(path.read_text())
            manifest["artifacts"] = []
            write_json(path, manifest)
    before = snapshot(tree)

    class NeverFetch:
        calls = 0

        def get(self, _url):
            self.calls += 1
            raise AssertionError("corrupt manifests must fail before fetching")

    with pytest.raises(RuntimeError, match="family manifests disagree"):
        validate_tree(tree, strict=True)
    upstream = NeverFetch()
    attempt = {}
    with pytest.raises(RuntimeError, match="family manifests disagree"):
        synchronize(tree, upstream, strict=True, attempt=attempt)
    assert upstream.calls == 0
    assert attempt["publication_eligible"] is False
    assert snapshot(tree) == before


def test_legacy_layout_without_family_manifests_remains_readable(tree):
    coverage_path = tree / "state/source-coverage.json"
    coverage = json.loads(coverage_path.read_text())
    coverage["schema_version"] = 1
    write_json(coverage_path, coverage)
    for family in ("docs.typesafe.ai", "github.typesafe-ai"):
        (tree / f"sources/{family}/manifest.json").unlink()
    validate_tree(tree, strict=True)


def test_empty_documentation_cannot_claim_complete_coverage(tree):
    coverage = json.loads((tree / "state/source-coverage.json").read_text())
    coverage["pages"] = []
    for key in ("discovered_count", "fetched_count", "retained_count"):
        coverage[key] = 0
    with pytest.raises(RuntimeError, match="no documentation pages"):
        validate_coverage(coverage)


@pytest.mark.parametrize("schema", [None, 3, "2", True, 2.0])
def test_unknown_coverage_schema_cannot_select_legacy_compatibility(schema):
    coverage = {} if schema is None else {"schema_version": schema}
    with pytest.raises(RuntimeError, match="unsupported source coverage schema"):
        validate_coverage(coverage)


def test_unknown_schema_with_missing_states_aborts_before_fetch_and_history(tree):
    path = tree / "state/source-coverage.json"
    coverage = json.loads(path.read_text())
    coverage["schema_version"] = 3
    write_json(path, coverage)
    for name in ("api", "models", "primitives", "model-limitations"):
        (tree / f"state/{name}.json").unlink()
    for family in ("docs.typesafe.ai", "github.typesafe-ai"):
        (tree / f"sources/{family}/manifest.json").unlink()
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("unsupported schemas must fail before fetching")

    with pytest.raises(RuntimeError, match="unsupported source coverage schema"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize("ancestor", ["state", "sources", "events"])
def test_json_error_hides_checkout_ancestors_named_like_generated_directories(tmp_path, ancestor):
    path = tmp_path / ancestor / "private-checkout" / "state/input.json"
    path.parent.mkdir(parents=True)
    path.write_text("malformed private body")
    with pytest.raises(RuntimeError) as caught:
        read_json(path)
    assert str(caught.value) == "cannot read persisted JSON: state/input.json"


@pytest.mark.parametrize(
    ("relative", "field", "corrupt"),
    [
        ("state/sources.json", "schema_version", 2),
        ("state/sources.json", "scope", "unrelated public/private sources"),
        ("state/sources.json", "documentation_license_note", "Redistribution permitted"),
        ("state/sources.json", "artifacts", {}),
        ("sources/docs.typesafe.ai/manifest.json", "schema_version", 2),
        ("sources/docs.typesafe.ai/manifest.json", "source", "https://unrelated.invalid"),
        ("sources/docs.typesafe.ai/manifest.json", "artifacts", {}),
        ("sources/github.typesafe-ai/manifest.json", "schema_version", 2),
        ("sources/github.typesafe-ai/manifest.json", "source", "https://unrelated.invalid"),
        ("sources/github.typesafe-ai/manifest.json", "artifacts", {}),
    ],
)
def test_manifest_envelopes_cannot_relabel_valid_artifacts(tree, relative, field, corrupt):
    path = tree / relative
    value = json.loads(path.read_text())
    value[field] = corrupt
    write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("invalid provenance must fail before fetching")

    with pytest.raises(RuntimeError, match="invalid source manifest envelope"):
        validate_tree(tree, strict=True)
    with pytest.raises(RuntimeError, match="invalid source manifest envelope"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


def test_swapped_source_families_cannot_create_false_documentation_additions(tree):
    docs_path = tree / "sources/docs.typesafe.ai/manifest.json"
    github_path = tree / "sources/github.typesafe-ai/manifest.json"
    docs = json.loads(docs_path.read_text())
    github = json.loads(github_path.read_text())
    di = next(i for i, r in enumerate(docs["artifacts"]) if r["source_type"] == "documentation")
    gi = next(i for i, r in enumerate(github["artifacts"]) if r["source_type"] == "github-api")
    docs["artifacts"][di], github["artifacts"][gi] = github["artifacts"][gi], docs["artifacts"][di]
    write_json(docs_path, docs)
    write_json(github_path, github)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("misclassified history must fail before fetching")

    with pytest.raises(RuntimeError, match="source record belongs to wrong family"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/llms.txt",
        "https://evil.example/llms.txt",
        "http://docs.typesafe.ai/llms.txt",
    ],
)
def test_coordinated_discovery_relabeling_cannot_escape_official_https_origin(tree, url):
    path = tree / "state/source-coverage.json"
    coverage = json.loads(path.read_text())
    sid = coverage["discovery"]["source_id"]
    coverage["discovery"]["url"] = url
    write_json(path, coverage)
    for relative in ("state/sources.json", "sources/docs.typesafe.ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        record["fetch_url"] = record["canonical_url"] = url
        write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("untrusted discovery must fail before fetching")

    with pytest.raises(RuntimeError):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    "sid",
    [
        "github:typesafe-ai/skills:commit",
        "skill:typesafe-ai",
        "registry:pypi:typesafe-sdk",
        "registry:npm:@typesafe-ai/sdk",
        "github:typesafe-ai/system-one-adapter-python:README.md",
    ],
)
@pytest.mark.parametrize("mutation", ["http-host", "wrong-official-identity", "wrong-type"])
def test_coordinated_github_record_corruption_cannot_relabel_provenance(tree, sid, mutation):
    for relative in ("state/sources.json", "sources/github.typesafe-ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        if mutation == "wrong-type":
            record["source_type"] = "unrecognized-source"
        else:
            url = (
                "http://evil.example/evidence"
                if mutation == "http-host"
                else record["fetch_url"].replace("typesafe", "unrelated")
            )
            record["fetch_url"] = record["canonical_url"] = url
        write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("relabeled provenance must fail before fetching")

    with pytest.raises(RuntimeError, match="source identity URL mismatch"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    "sid",
    [
        "github:typesafe-ai/typesafe-sdk-python:head",
        "github:typesafe-ai/typesafe-sdk-python:repository",
        "github:typesafe-ai/typesafe-sdk-python:tags",
        "github:typesafe-ai/typesafe-sdk-python:releases",
        "github:typesafe-ai/skills:commit",
        "github:typesafe-ai/system-one-adapter-python:tags",
    ],
)
def test_api_identities_cannot_be_reclassified_as_immutable_raw_files(tree, sid):
    _, repo, identity = sid.split(":", 2)
    commit = "c" * 40
    for relative in ("state/sources.json", "sources/github.typesafe-ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        record.update(
            source_type=(
                "official_evaluation_tool" if "system-one-adapter" in repo else "github-source"
            ),
            upstream_commit=commit,
            fetch_url=f"https://raw.githubusercontent.com/{repo}/{commit}/{identity}",
            canonical_url=f"https://raw.githubusercontent.com/{repo}/{commit}/{identity}",
        )
        write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("role-switched provenance must fail before fetching")

    with pytest.raises(RuntimeError, match="source identity URL mismatch"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    ("sid", "field"),
    [
        ("registry:pypi:typesafe-sdk", "upstream_commit"),
        ("registry:pypi:typesafe-sdk", "source_tag"),
        ("registry:npm:@typesafe-ai/sdk", "upstream_commit"),
        ("registry:npm:@typesafe-ai/sdk", "source_tag"),
        ("docs:page", "upstream_commit"),
        ("docs:page", "source_tag"),
        ("docs:llms.txt", "upstream_commit"),
        ("docs:llms.txt", "source_tag"),
        ("skill:typesafe-ai", "source_tag"),
    ],
)
def test_mutable_and_commit_only_sources_cannot_claim_unverified_release_metadata(tree, sid, field):
    family = "docs.typesafe.ai" if sid.startswith("docs:") else "github.typesafe-ai"
    for relative in ("state/sources.json", f"sources/{family}/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        record[field] = "c" * 40 if field == "upstream_commit" else "v99.0.0"
        write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("unsupported attribution must fail before fetching")

    with pytest.raises(RuntimeError, match="source identity URL mismatch"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


def change_tagged_commit(tree, sid):
    for relative in ("state/sources.json", "sources/github.typesafe-ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        previous = record["upstream_commit"]
        record["upstream_commit"] = "f" * 40
        record["fetch_url"] = record["fetch_url"].replace(previous, "f" * 40)
        record["canonical_url"] = record["canonical_url"].replace(previous, "f" * 40)
        write_json(path, value)


@pytest.mark.parametrize(
    "repo",
    ["typesafe-sdk-python", "typesafe-sdk-js", "system-one-adapter-python"],
)
def test_tagged_sources_must_match_retained_release_commits(tree, repo):
    sources = json.loads((tree / "state/sources.json").read_text())["artifacts"]
    sid = next(
        r["source_id"]
        for r in sources
        if r["source_id"].startswith(f"github:typesafe-ai/{repo}:")
        and r.get("source_tag") is not None
        and r.get("upstream_commit") is not None
    )
    change_tagged_commit(tree, sid)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("incorrect tag/commit attribution must fail before fetching")

    with pytest.raises(RuntimeError, match="tagged source disagrees with retained release binding"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize("legacy_candidate", [False, True])
def test_held_candidate_bindings_validate_and_upgrade_without_false_history(
    tmp_path, monkeypatch, legacy_candidate
):
    class GitAhead(PublicSources):
        ahead = False

        def get(self, url):
            if self.ahead and "typesafe-sdk-python/tags?" in url:
                return Response(
                    url,
                    json.dumps(
                        [
                            {"name": "v0.1.0", "commit": {"sha": "a" * 40}},
                            {"name": "v0.2.0", "commit": {"sha": "c" * 40}},
                        ]
                    ).encode(),
                    {},
                )
            return super().get(url)

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    upstream = GitAhead()
    synchronize(tmp_path, upstream, strict=True)
    trusted = (tmp_path / "state/sdk-python.json").read_bytes()
    upstream.ahead = True
    synchronize(tmp_path, upstream, strict=True)
    if legacy_candidate:
        path = tmp_path / "state/freshness.json"
        freshness = json.loads(path.read_text())
        freshness["python"].pop("candidate_tag_bindings")
        freshness["fingerprint"] = sha256_json(
            {
                key: value
                for key, value in freshness.items()
                if key
                not in {"observed_at", "fingerprint", "last_changed", "last_successful_observation"}
            }
        )
        write_json(path, freshness)
        validate_tree(tmp_path, strict=True)
        assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert (tmp_path / "state/sdk-python.json").read_bytes() == trusted
    validate_tree(tmp_path, strict=True)
    sources = json.loads((tmp_path / "state/sources.json").read_text())["artifacts"]
    sid = next(
        r["source_id"]
        for r in sources
        if r["source_id"].startswith("candidate:github:typesafe-ai/typesafe-sdk-python:")
        and r.get("source_tag") is not None
    )
    change_tagged_commit(tmp_path, sid)
    before = snapshot(tmp_path)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("incorrect candidate binding must fail before fetching")

    with pytest.raises(RuntimeError, match="tagged source disagrees with retained release binding"):
        synchronize(tmp_path, NeverFetch(), strict=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "mutation", ["raw-hash", "raw-commit", "head-hash", "missing-raw", "missing-head"]
)
def test_skill_snapshot_binds_raw_artifact_and_commit_observation(tree, mutation):
    sid = "github:typesafe-ai/skills:commit" if "head" in mutation else "skill:typesafe-ai"
    for relative in ("state/sources.json", "sources/github.typesafe-ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        if mutation.startswith("missing"):
            value["artifacts"].remove(record)
        elif mutation == "raw-commit":
            previous = record["upstream_commit"]
            record["upstream_commit"] = "f" * 40
            record["fetch_url"] = record["fetch_url"].replace(previous, "f" * 40)
            record["canonical_url"] = record["canonical_url"].replace(previous, "f" * 40)
        else:
            record["sha256"] = "0" * 64
        write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("conflicting skill evidence must fail before fetching")

    with pytest.raises(RuntimeError, match="skill provenance disagrees"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_sdk_head_hashes_must_match_their_retained_main_observations(tree, kind):
    trusted = (tree / f"state/sdk-{kind}.json").read_bytes()
    repo = "typesafe-sdk-python" if kind == "python" else "typesafe-sdk-js"
    sid = f"github:typesafe-ai/{repo}:head"
    for relative in ("state/sources.json", "sources/github.typesafe-ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(r for r in value["artifacts"] if r["source_id"] == sid)
        record["sha256"] = "0" * 64
        write_json(path, value)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("inconsistent main observation must fail before fetching")

    with pytest.raises(RuntimeError, match="SDK main provenance disagrees"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before
    assert (tree / f"state/sdk-{kind}.json").read_bytes() == trusted


@pytest.mark.parametrize("kind", ["python", "javascript"])
def test_legacy_untagged_sdk_config_retains_its_snapshot_url_binding(tree, kind):
    repo = "typesafe-sdk-python" if kind == "python" else "typesafe-sdk-js"
    filename = "pyproject.toml" if kind == "python" else "package.json"
    legacy_id = f"github:typesafe-ai/{repo}:{filename}"
    original_id = None
    for relative in ("state/sources.json", "sources/github.typesafe-ai/manifest.json"):
        path = tree / relative
        value = json.loads(path.read_text())
        record = next(
            r
            for r in value["artifacts"]
            if r["source_id"].startswith(f"github:typesafe-ai/{repo}:")
            and r["source_id"].endswith(":" + filename)
            and r.get("source_tag")
        )
        original_id = record["source_id"]
        record["source_id"] = legacy_id
        record["source_tag"] = None
        write_json(path, value)
    path = tree / f"state/sdk-{kind}.json"
    sdk = json.loads(path.read_text())
    sdk.pop("release_evidence_policy")
    for citation in sdk["provenance"]:
        if citation["source_id"] == original_id:
            citation["source_id"] = legacy_id
    sdk["fingerprint"] = sha256_json(
        {key: value for key, value in sdk.items() if key not in {"observed_at", "fingerprint"}}
    )
    write_json(path, sdk)
    validate_tree(tree, strict=True)
    change_tagged_commit(tree, legacy_id)
    before = snapshot(tree)

    class NeverFetch:
        def get(self, _url):
            raise AssertionError("retargeted legacy evidence must fail before fetching")

    with pytest.raises(RuntimeError, match="snapshot citation disagrees with retained source"):
        synchronize(tree, NeverFetch(), strict=True)
    assert snapshot(tree) == before
