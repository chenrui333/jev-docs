import pytest
from test_transactions import PublicSources, snapshot

from jev_docs.sync import Artifact, derive_state, synchronize

REFERENCE = """# API reference
## Evaluation endpoint
```http
POST https://api.typesafe.ai/v1/systemone
```
## Request body
<ParamField body="state" type="string | object | array" required>Content</ParamField>
<ParamField body="model" type="string" required>Use "jev-latest".</ParamField>
<ParamField body="questions" type="map<string, Question>" required>
<Expandable title="entries"><ParamField body="id" type="Question">Nested</ParamField></Expandable>
</ParamField>
## Question types
### Choice
<ParamField body="type" type="&#x22;choice&#x22;" required />
### Noul
<ParamField body="type" type="&#x22;noul&#x22;" required />
### Score
<ParamField body="type" type="&#x22;score&#x22;" required />
## Response body
<ResponseField name="answers" type="map<string, Answer>" required>Same ids you provided.</ResponseField>
"""


def derive(text=REFERENCE, previous=None):
    artifacts = (
        {}
        if text is None
        else {
            "docs:api": Artifact(
                "docs:api", "documentation", "https://docs.typesafe.ai/api.md", text.encode()
            )
        }
    )
    return derive_state({} if text is None else {"docs:api": text}, artifacts, {"api": previous})[
        "api"
    ]


def test_required_model_is_not_a_default_inferred_from_an_example():
    api = derive()
    assert api["model_default"] is None
    assert api["model_required"] is True
    assert api["required_fields"] == ["model", "questions", "state"]
    assert api["request_fields"] == ["model", "questions", "state"]
    assert api["response_shape"] == "answers map keyed by question id"
    assert api["properties"]["model_required"]["status"] == "verified"
    assert api["properties"]["model_default"]["status"] == "unknown"
    assert api["question_types"] == ["choice", "noul", "score"]
    # Extraction normalizes entity-encoded literals, but evidence stays verbatim.
    for evidence in api["properties"]["question_types"]["evidence"]:
        assert evidence["excerpt"] in REFERENCE
        assert "&#x22;" in evidence["excerpt"]


def test_optional_default_requires_an_explicit_declaration():
    optional = REFERENCE.replace(
        'body="model" type="string" required', 'body="model" type="string" default="jev-optional"'
    )
    api = derive(optional)
    assert api["model_required"] is False
    assert api["model_default"] == "jev-optional"
    assert api["endpoint"] == derive()["endpoint"]
    assert api["question_types"] == derive()["question_types"]


def test_conflicting_declarations_are_unknown_not_arbitrarily_selected():
    page = REFERENCE.replace(
        "## Question types",
        '<ParamField body="model" type="string">Optional</ParamField>\n## Question types',
    )
    api = derive(page)
    assert api["model_required"] is None
    assert api["model_default"] is None
    assert api["properties"]["model_required"]["status"] == "unknown"
    assert api["endpoint"] == derive()["endpoint"]


def test_missing_citations_remain_historical_and_warn_until_restored():
    old = derive()
    old["provenance"].append(
        {
            "source_id": "docs:migrating-to-v1",
            "url": "https://docs.typesafe.ai/migrating-to-v1",
            "sha256": "a" * 64,
        }
    )
    api = derive(previous=old)
    assert api["missing_sources"][0]["source_id"] == "docs:migrating-to-v1"
    assert any("disappeared" in w for w in api["warnings"])
    assert derive(previous=api)["warnings"] == api["warnings"]
    missing = derive(None, api)
    assert missing["endpoint"] is None
    assert missing["properties"]["endpoint"]["last_known"]["value"] == old["endpoint"]
    assert derive(None, missing)["properties"] == missing["properties"]
    restored = derive(previous=missing)
    assert restored["properties"]["endpoint"]["status"] == "verified"


def test_legacy_curated_default_is_not_promoted_to_verified_history():
    api = derive(previous={"model_default": "jev-latest", "provenance": []})
    known = api["properties"]["model_default"]["last_known"]
    assert known["status"] == "curated_unverified"
    assert api["model_default"] is None


def test_api_transaction_is_idempotent_and_fetch_failure_preserves_tree(tmp_path):
    upstream = PublicSources()
    upstream.pages["api"] = REFERENCE
    synchronize(tmp_path, upstream, strict=True)
    before = snapshot(tmp_path)
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == before
    upstream.fail_page = True
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == before
