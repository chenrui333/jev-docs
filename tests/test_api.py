import pytest
from test_transactions import PublicSources, snapshot

from jev_docs.sync import Artifact, derive_state, semantic_events, synchronize

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
    response_evidence = api["properties"]["response_shape"]["evidence"]
    assert all(e["excerpt"] in REFERENCE for e in response_evidence)
    assert any("same ids" in e["excerpt"].lower() for e in response_evidence)


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
    assert api["request_fields"] == ["model", "questions", "state"]
    assert api["required_fields"] is None


@pytest.mark.parametrize(
    "declaration",
    [
        'body="type" type="string"',
        'body="type"',
        "body='type' type='\"unknown\"'",
        'body="type" type={questionType}',
        'body="type" type="&#x22;other&#x22;" extra=\'unsupported\'',
        'body="type" type="&#x22;other&#x22;" type="&#x22;duplicate&#x22;"',
        'body="type" type="unterminated',
    ],
)
def test_unknown_type_declaration_does_not_verify_a_partial_list(declaration):
    page = REFERENCE.replace("## Response body", f"<ParamField {declaration} />\n## Response body")
    api = derive(page)
    assert api["question_types"] is None
    assert any("question_types is unknown" in w for w in api["warnings"])


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ('type="string required value"', False),
        ('type="string" required={true}', True),
        ('type="string" required={false}', False),
        ('type="string" required={isRequired}', None),
        ('type="string" required="false"', None),
        ("type='string' required", None),
    ],
)
def test_requiredness_uses_attributes_not_quoted_words(attribute, expected):
    page = REFERENCE.replace('type="string" required>Use', f"{attribute}>Use")
    api = derive(page)
    assert api["model_required"] is expected
    assert api["model_default"] is None
    if expected is None:
        assert any("model_required is unknown" in w for w in api["warnings"])


@pytest.mark.parametrize(
    "sentence",
    [
        "Answers do not use the same ids you provided.",
        "Not the same ids you provided.",
        "Same ids you provided are not guaranteed.",
        "One answer per question, not returned under the same ids you provided.",
        "Do not assume: Same ids you provided.",
        'An example says "Same ids you provided."',
    ],
)
def test_nonaffirmative_response_correlation_is_unknown(sentence):
    api = derive(REFERENCE.replace("Same ids you provided.", sentence))
    assert api["response_shape"] is None
    assert any("response_shape is unknown" in w for w in api["warnings"])


def test_fenced_examples_do_not_establish_declarations_or_correlation():
    page = REFERENCE.replace(
        "## Question types",
        '```mdx\n<ParamField body="example" required />\n```\n## Question types',
    ).replace(
        "## Response body",
        '~~~mdx\n<ParamField body="type" type="&#x22;example&#x22;" />\n~~~\n## Response body',
    )
    api = derive(page)
    assert api["request_fields"] == ["model", "questions", "state"]
    assert api["question_types"] == ["choice", "noul", "score"]
    fenced = REFERENCE.replace("Same ids you provided.", "\n```text\nSame ids you provided.\n```\n")
    assert derive(fenced)["response_shape"] is None


@pytest.mark.parametrize(
    "sentence",
    [
        "Same ids you provided.",
        "One answer per question, returned under the same ids you provided.",
        "One [Answer](#answer-types) per question, keyed by the same ids you used in questions.",
    ],
)
def test_affirmative_response_evidence_is_verbatim(sentence):
    page = REFERENCE.replace("Same ids you provided.", sentence)
    api = derive(page)
    assert api["response_shape"] == "answers map keyed by question id"
    assert all(e["excerpt"] in page for e in api["properties"]["response_shape"]["evidence"])


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


def test_curated_to_verified_migration_is_tracking_not_upstream_history():
    current = derive()
    legacy = {
        "endpoint": current["endpoint"],
        "request_fields": ["model", "state", "questions"],
        "question_types": current["question_types"],
        "model_default": "jev-latest",
        "response_shape": current["response_shape"],
        "provenance": current["provenance"],
    }
    events = semantic_events({"api": legacy}, {"api": current}, "2026-10-18")
    assert len(events) == 1
    assert events[0]["change"] == "baseline"
    assert "corrected repository assumptions" in events[0]["summary"]
    assert semantic_events({"api": current}, {"api": derive(previous=current)}, "later") == []


@pytest.mark.parametrize("old_hash", ["a" * 64, None])
def test_first_verified_snapshot_does_not_deny_unknown_upstream_contract_changes(old_hash):
    current = derive()
    source = {**current["provenance"][0], "sha256": old_hash}
    legacy = {"model_default": "jev-latest", "provenance": [source]}
    events = semantic_events({"api": legacy}, {"api": current}, "2026-10-18")
    assert len(events) == 1
    assert events[0]["change"] == "baseline"
    summary = events[0]["summary"]
    assert "first verified snapshot" in summary
    assert "no historical API behavior inferred" in summary
    assert "not an upstream" not in summary
    if old_hash:
        assert "upstream source changed" in summary


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
