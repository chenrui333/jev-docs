import hashlib

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
"""
# Exact, bounded Response body section from the reviewed official API page.
RESPONSE = """
One answer per question, returned under the same ids you provided.

<ResponseField name="model" type="string" required>
  The model that performed the evaluation.
</ResponseField>

<ResponseField name="answers" type="map<string, Answer>" required>
  One [Answer](#answer-types) per question, keyed by the same ids you used in questions.

  <Expandable title="map entries">
    <ResponseField name="‹question id›" type="Answer">
      The same id you chose in questions.
    </ResponseField>
  </Expandable>
</ResponseField>

<ResponseField name="usage" type="object" required>
  Token usage for the request.

  <Expandable title="properties">
    <ResponseField name="input_tokens" type="integer" />

    <ResponseField name="output_tokens" type="integer" />
  </Expandable>
</ResponseField>

```json Example response theme={null}
{
  "model": "jev-1.13.0",
  "answers": {
    "is_urgent": {
      "type": "noul",
      "noul": 0.95
    }
  },
  "usage": { "input_tokens": 296, "output_tokens": 20 }
}
```
"""
REFERENCE += "## Response body\n" + RESPONSE


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
    api = derive(REFERENCE.replace("returned under the same ids you provided.", sentence))
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
    fenced = REFERENCE.replace(
        "returned under the same ids you provided.", "\n```text\nSame ids you provided.\n```\n"
    )
    assert derive(fenced)["response_shape"] is None


@pytest.mark.parametrize(
    "addition",
    [
        "Answer IDs differ from question IDs.",
        "Answers aren't keyed by question IDs.",
        "Same ids you provided.",
        "An unrelated usage note.",
    ],
)
def test_unreviewed_response_context_is_unknown_even_with_familiar_affirmation(addition):
    page = REFERENCE + "\n" + addition + "\n"
    api = derive(page)
    assert api["response_shape"] is None
    prop = api["properties"]["response_shape"]
    assert prop["method"] == "reviewed_reference_interpretation"
    assert "review" in prop["reason"].lower()
    assert any("response_shape is unknown" in w for w in api["warnings"])
    for name in (
        "endpoint",
        "request_fields",
        "required_fields",
        "model_required",
        "question_types",
    ):
        assert api[name] == derive()[name]


def test_reviewed_response_interpretation_has_verbatim_evidence_and_section_hash():
    api = derive()
    prop = api["properties"]["response_shape"]
    assert prop["method"] == "reviewed_reference_interpretation"
    normalized = "\n".join(line.rstrip() for line in RESPONSE.splitlines()).strip()
    assert prop["reviewed_section_sha256"] == hashlib.sha256(normalized.encode()).hexdigest()
    assert prop["observed_section_sha256"] == prop["reviewed_section_sha256"]
    assert "repository interpretation" in prop["interpretation"]
    assert api["response_shape"] == "answers map keyed by question id"
    assert all(e["excerpt"] in REFERENCE and len(e["excerpt"]) <= 240 for e in prop["evidence"])


def test_review_scope_includes_fenced_response_examples_but_not_other_sections():
    page = REFERENCE.replace('"output_tokens": 20', '"output_tokens": 21')
    assert derive(page)["response_shape"] is None
    assert (
        derive(REFERENCE + "\n## Other section\nUnreviewed prose.")["response_shape"]
        == derive()["response_shape"]
    )
    whitespace_only = REFERENCE.replace("\n", "  \r\n")
    assert derive(whitespace_only)["response_shape"] == derive()["response_shape"]


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


@pytest.mark.parametrize(
    "page",
    [
        None,
        REFERENCE.replace(
            'body="model" type="string" required',
            'body="model" type="string" required={unsupported}',
        ),
    ],
)
def test_api_verification_loss_and_recovery_are_not_contract_changes(page):
    verified = derive()
    unknown = derive(page, verified)
    events = semantic_events({"api": verified}, {"api": unknown}, "loss")
    assert len(events) == 1
    assert events[0]["change"] == "verification_changed"
    assert "verification" in events[0]["summary"].lower()
    historical = [s for s in events[0]["sources"] if s.get("role") == "historical"]
    assert historical
    assert historical[0]["sha256"] == verified["provenance"][0]["sha256"]
    repeated = derive(page, unknown)
    assert semantic_events({"api": unknown}, {"api": repeated}, "repeat") == []
    recovery = semantic_events({"api": repeated}, {"api": derive(previous=repeated)}, "recovery")
    assert len(recovery) == 1
    assert recovery[0]["change"] == "verification_changed"
    changed = derive(
        REFERENCE.replace(
            "https://api.typesafe.ai/v1/systemone", "https://api.typesafe.ai/v2/systemone"
        )
    )
    assert semantic_events({"api": verified}, {"api": changed}, "change")[0]["change"] == "modified"


@pytest.mark.parametrize(
    "negative",
    [
        "Answers do not use the same ids you provided.",
        "The same ids you provided are not guaranteed.",
        "Answers use different keys from the questions.",
    ],
)
def test_conflicting_response_correlation_is_unknown(negative):
    page = REFERENCE.replace("## Response body", "## Response body\n" + negative + "\n")
    assert derive(page)["response_shape"] is None


def test_long_declarations_cannot_verify_with_clipped_evidence():
    padding = 'description="' + "x" * 250 + '" '
    page = REFERENCE.replace('<ParamField body="model"', "<ParamField " + padding + 'body="model"')
    page = page.replace('<ParamField body="type"', "<ParamField " + padding + 'body="type"')
    page = page.replace(
        '<ResponseField name="answers"', "<ResponseField " + padding + 'name="answers"'
    )
    api = derive(page)
    for name in (
        "model_required",
        "request_fields",
        "required_fields",
        "question_types",
        "response_shape",
    ):
        assert api["properties"][name]["status"] == "unknown"
        assert any(name + " is unknown" in w for w in api["warnings"])
    assert all(e["excerpt"] in page for p in api["properties"].values() for e in p["evidence"])


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


@pytest.mark.parametrize(
    "title,claim",
    [("Request body", "request_fields"), ("Response body", "response_shape")],
)
def test_duplicate_authoritative_sections_do_not_hide_conflicts(title, claim):
    page = REFERENCE + f"\n## {title}\nConflicting, unreviewed declarations.\n"
    api = derive(page)
    assert api["properties"][claim]["status"] == "unknown"
    assert api[claim] is None
    assert api["endpoint"] == derive()["endpoint"]


def test_nested_map_entries_are_not_promoted_to_request_parameters():
    nested = (
        '<Expandable title="entries"><Expandable title="inner">'
        '<ParamField body="nested" type="string" /></Expandable>'
    )
    page = REFERENCE.replace('<Expandable title="entries">', nested)
    api = derive(page)
    assert api["request_fields"] == ["model", "questions", "state"]
    assert api["required_fields"] == ["model", "questions", "state"]


def test_unbalanced_map_entry_markup_makes_request_contract_unknown():
    api = derive(REFERENCE.replace("</Expandable>", "", 1))
    assert api["request_fields"] is None
    assert api["model_required"] is None
    assert api["endpoint"] == derive()["endpoint"]


@pytest.mark.parametrize("text", [None, "# Unrecognized API reference layout\n"])
def test_migration_without_verified_facts_is_unknown_evidence_not_verified_baseline(text):
    original = derive()
    legacy = {
        "endpoint": original["endpoint"],
        "model_default": "jev-latest",
        "provenance": original["provenance"],
    }
    unknown = derive(text, legacy)
    assert all(p["status"] == "unknown" for p in unknown["properties"].values())
    events = semantic_events({"api": legacy}, {"api": unknown}, "2026-10-18")
    assert len(events) == 1
    assert events[0]["change"] == "verification_changed"
    assert any(s["role"] == "historical" for s in events[0]["sources"])
    assert semantic_events({"api": unknown}, {"api": derive(text, unknown)}, "later") == []


@pytest.mark.parametrize(
    "wrap",
    [
        lambda tag: f"<!-- {tag} -->",
        lambda tag: f"`{tag}`",
        lambda tag: f"\n    {tag}\n",
        lambda tag: f"\n\t{tag}\n",
        lambda tag: f"<code>{tag}</code>",
        lambda tag: f'<PRE class="example">{tag}</PRE>',
        lambda tag: f"> {tag}",
        lambda tag: f"> ~~~mdx\n> {tag}\n> ~~~",
        lambda tag: f"  >> ~~~mdx\n  >> {tag}\n  >> ~~~",
        lambda tag: f"> > ```mdx\n> > {tag}\n> > ```",
    ],
)
def test_inactive_parameter_examples_do_not_manufacture_request_contract(wrap):
    ghost = '<ParamField body="ghost" type="string" required />'
    page = REFERENCE.replace("## Request body", "## Request body\n" + wrap(ghost))
    api = derive(page)
    assert api["request_fields"] == ["model", "questions", "state"]
    assert api["required_fields"] == ["model", "questions", "state"]


def test_commented_discriminator_does_not_invalidate_current_question_types():
    page = REFERENCE.replace(
        "## Question types",
        '## Question types\n<!-- <ParamField body="type" type="string" /> -->',
    )
    assert derive(page)["question_types"] == ["choice", "noul", "score"]


def test_commented_endpoint_does_not_override_active_http_declaration():
    page = REFERENCE.replace(
        "## Evaluation endpoint",
        "## Evaluation endpoint\n<!--\nPOST https://retired.example/api\n-->",
    )
    assert derive(page)["endpoint"] == derive()["endpoint"]


def test_unclosed_comment_cannot_verify_inactive_reference_sections():
    page = REFERENCE.replace("## Request body", "<!--\n## Request body")
    api = derive(page)
    assert api["request_fields"] is None
    assert api["question_types"] is None
    assert api["response_shape"] is None
    assert api["endpoint"] == derive()["endpoint"]


def test_complete_reference_quoted_as_code_is_not_current_api_evidence():
    api = derive("````markdown\n" + REFERENCE + "\n````")
    assert all(prop["status"] == "unknown" for prop in api["properties"].values())


@pytest.mark.parametrize(
    "recovered_text",
    [
        REFERENCE.replace("/v1/systemone", "/v2/systemone"),
        REFERENCE.replace('body="model" type="string" required', 'body="model" type="string"'),
    ],
)
def test_recovery_compares_with_last_verified_facts_across_unknown_gap(recovered_text):
    original = derive()
    unavailable = derive(None, original)
    recovered = derive(recovered_text, unavailable)
    events = semantic_events({"api": unavailable}, {"api": recovered}, "2026-10-18")
    assert len(events) == 1
    assert events[0]["change"] == "modified"
    assert any(
        s["role"] == "historical" and s["sha256"] == original["provenance"][0]["sha256"]
        for s in events[0]["sources"]
    )
    assert (
        semantic_events({"api": recovered}, {"api": derive(recovered_text, recovered)}, "later")
        == []
    )
