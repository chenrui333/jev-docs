"""Reviewed complete contexts and literal bundles govern recommendations, not keywords."""

import json
import re
from pathlib import Path

import pytest
from test_transactions import PublicSources, snapshot

from jev_docs.sync import (
    PRACTICE_RULES,
    Artifact,
    Response,
    derive_practices,
    observed_state,
    render_best_practices,
    semantic_events,
    sha256_bytes,
    synchronize,
    validate_tree,
)
from jev_docs.text import clean_text

PASSAGES = json.loads((Path(__file__).parent / "fixtures/practice-passages.json").read_text())


def artifacts(contents):
    return {
        sid: Artifact(
            sid,
            "github-source" if sid.startswith("skill:") else "documentation",
            PASSAGES[sid]["url"],
            text.encode(),
        )
        for sid, text in contents.items()
    }


def items(state):
    return {item["id"]: item for item in state["items"]}


def decisions(sid, supported=(), rejected=()):
    return {
        rule["id"]: "supported"
        if rule["id"] in supported
        else "rejected"
        if rule["id"] in rejected
        else "non_conflicting"
        for rule in PRACTICE_RULES
        if sid in rule["sources"]
    }


@pytest.fixture(autouse=True)
def reviewed_passage_fixtures(review_contexts, monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    contents = {sid: fixture["text"] for sid, fixture in PASSAGES.items()}
    review_contexts(
        contents,
        artifacts(contents),
        {sid: decisions(sid, fixture["recommended"]) for sid, fixture in PASSAGES.items()},
    )


def reviewed_blocks(rule):
    blocks = {}
    required = {digest for digest, _claim in rule["claims"]}
    for sid in rule["sources"]:
        if sid not in PASSAGES:
            continue
        for raw in re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |\|))", clean_text(PASSAGES[sid]["text"])):
            block = re.sub(r"\s+", " ", re.sub(r"^\s*[-*]\s+", "", raw)).strip()
            digest = sha256_bytes(block.encode())
            if digest in required:
                blocks[digest] = block
    assert set(blocks) == required
    return blocks


@pytest.mark.parametrize("sid", PASSAGES)
def test_reviewed_positive_passages_support_exactly_the_expected_rules(sid):
    contents = {sid: PASSAGES[sid]["text"]}
    provenance = artifacts(contents)
    state = derive_practices(contents, provenance, None)
    expected = set(PASSAGES[sid]["recommended"])
    assert {item["id"] for item in state["items"] if item["status"] == "recommended"} == expected
    normalized = " ".join(clean_text(contents[sid]).split())
    for item in state["items"]:
        for source in item["sources"]:
            assert source["source_id"] == sid
            assert source["url"] == PASSAGES[sid]["url"]
            assert source["sha256"] == provenance[sid].sha256
            assert source["excerpt"] in normalized
            assert len(source["excerpt"]) <= 420
            assert not source["excerpt"].endswith(" …")
    assert expected <= {rule["id"] for rule in PRACTICE_RULES}


def test_current_evidence_rebuilds_facts_despite_stale_previous_fingerprint(monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    contents = {sid: fixture["text"] for sid, fixture in PASSAGES.items()}
    evidence = artifacts(contents)
    fresh = derive_practices(contents, evidence, None)
    previous = json.loads(json.dumps(fresh))
    for item in previous["items"]:
        if item["status"] == "recommended":
            item["summary"] = "Unsupported persisted recommendation."
    # Treat the fingerprint as a change-detection hint, never authority for facts.
    rebuilt = derive_practices(contents, evidence, previous)
    assert rebuilt == fresh


def test_positive_fixtures_cover_every_rule():
    assert {rule["id"] for rule in PRACTICE_RULES} == {
        rule_id for fixture in PASSAGES.values() for rule_id in fixture["recommended"]
    }


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ("<code>", "</code>"),
        ('<CoDe className="example">', "</CoDe>"),
        ("<pre>\n<code>", "</code>\n</pre>"),
        ("<PRE data-example>\n", "\n</PRE>"),
        ('<code title="/>">', "</code>"),
        ('<pre title="unterminated', ""),
        ("<blockquote>", "</blockquote>"),
        ("> <code>\n", "\n> </code>"),
        ("<code><code>", "</code></code>"),
        ("<code>", ""),
        ("<pre></code>\n\n", ""),
    ],
)
@pytest.mark.parametrize("sid", PASSAGES)
def test_raw_code_containers_are_not_guidance(sid, opening, closing):
    contents = {sid: opening + PASSAGES[sid]["text"] + closing}
    state = derive_practices(contents, artifacts(contents), None)
    assert all(item["status"] == "unknown" for item in state["items"])


def test_stricter_policy_reassessment_is_tracking_not_upstream_guidance_change():
    sid = "docs:confidence"
    rule = next(rule for rule in PRACTICE_RULES if rule["id"] == "domain-calibrated-thresholds")
    contents = {source_id: "Network delays have consequences." for source_id in rule["sources"]}
    provenance = artifacts(contents)
    legacy = derive_practices(contents, provenance, None)
    legacy.pop("evidence_policy")
    legacy.pop("source_observations")
    loose_match = items(legacy)["domain-calibrated-thresholds"]
    loose_match["status"] = "recommended"
    loose_match["sources"] = [
        {
            "source_id": sid,
            "url": provenance[sid].url,
            "sha256": provenance[sid].sha256,
            "excerpt": contents[sid],
        }
    ]
    current = derive_practices(contents, provenance, legacy)
    prior = {
        "practices": legacy,
        "sources": {
            "artifacts": [
                {"source_id": source_id, "sha256": artifact.sha256}
                for source_id, artifact in provenance.items()
            ]
        },
    }
    events = semantic_events(prior, {"practices": current}, "2026-10-18")
    assert len(events) == 1
    event = events[0]
    assert event["entity"] == loose_match["id"]
    assert event["change"] == "baseline"
    assert "recommended → unknown" in event["summary"]
    assert "tracking correction" in event["summary"]
    assert "not an upstream guidance change, removal or deprecation" in event["summary"]
    assert event["summary"] != loose_match["summary"]
    assert semantic_events({"practices": current}, {"practices": current}, "2026-10-19") == []


@pytest.mark.parametrize("source_change", ["removed", "rewritten", "missing-hash"])
def test_policy_migration_does_not_hide_genuine_source_evidence_changes(source_change):
    sid = "docs:primitives/noul"
    contents = {sid: PASSAGES[sid]["text"]}
    legacy = derive_practices(contents, artifacts(contents), None)
    legacy.pop("evidence_policy")
    legacy.pop("source_observations")
    if source_change == "removed":
        contents = {}
    else:
        contents = {sid: "The canonical guidance has changed."}
    if source_change == "missing-hash":
        for source in items(legacy)["noul-is-yes-probability"]["sources"]:
            source.pop("sha256")
    current = derive_practices(contents, artifacts(contents), legacy)
    events = semantic_events({"practices": legacy}, {"practices": current}, "2026-10-18")
    assert len(events) == 1
    assert events[0]["change"] == "modified"
    assert "recommended → unknown" in events[0]["summary"]
    assert "absence is not removal or deprecation" in events[0]["summary"]
    assert "tracking correction" not in events[0]["summary"]


def test_policy_marker_alone_does_not_create_practice_events():
    sid = "docs:primitives/noul"
    contents = {sid: PASSAGES[sid]["text"]}
    current = derive_practices(contents, artifacts(contents), None)
    legacy = {key: value for key, value in current.items() if key != "evidence_policy"}
    assert semantic_events({"practices": legacy}, {"practices": current}, "2026-10-18") == []


def test_later_evidence_loss_and_recovery_are_explicit_status_changes():
    sid = "docs:primitives/noul"
    contents = {sid: PASSAGES[sid]["text"]}
    supported = derive_practices(contents, artifacts(contents), None)
    absent = derive_practices({}, {}, supported)
    lost = semantic_events({"practices": supported}, {"practices": absent}, "2026-10-18")
    assert len(lost) == 1 and lost[0]["change"] == "modified"
    assert "recommended → unknown" in lost[0]["summary"]
    assert "Current support is insufficient" in lost[0]["summary"]
    assert "absence is not removal or deprecation" in lost[0]["summary"]
    assert lost[0]["summary"] != items(absent)["noul-is-yes-probability"]["summary"]
    restored = derive_practices(contents, artifacts(contents), absent)
    recovered = semantic_events({"practices": absent}, {"practices": restored}, "2026-10-19")
    assert len(recovered) == 1 and recovered[0]["change"] == "modified"
    assert "unknown → recommended" in recovered[0]["summary"]
    assert "Current canonical evidence supports the recommendation" in recovered[0]["summary"]
    assert semantic_events({"practices": restored}, {"practices": restored}, "2026-10-20") == []


def test_absent_or_ambiguous_evidence_is_historical_and_idempotent(monkeypatch):
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    contents = {sid: fixture["text"] for sid, fixture in PASSAGES.items()}
    prior = derive_practices(contents, artifacts(contents), None)
    assert all(item["status"] == "recommended" for item in prior["items"])
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    ambiguous = {sid: "Generic content: failures, consequences, concentration." for sid in contents}
    unknown = derive_practices(ambiguous, artifacts(ambiguous), prior)
    assert derive_practices(ambiguous, artifacts(ambiguous), unknown) == unknown
    absent = derive_practices({}, {}, unknown)
    assert absent["source_observations"] == {}
    assert derive_practices({}, {}, absent) == absent
    for item in absent["items"]:
        old = items(prior)[item["id"]]
        assert item["status"] == "unknown"
        assert item["sources"] == []
        assert item["historical_sources"] == old["sources"]
        assert item["first_seen"] == old["first_seen"]
        assert item["last_verified"] == old["last_verified"]
    rendered = render_best_practices(absent)
    assert "Unverified candidate:" in rendered
    assert "Historical evidence (not current support):" in rendered
    assert "— `recommended`" not in rendered
    restored = derive_practices(contents, artifacts(contents), absent)
    assert all(item["status"] == "recommended" for item in restored["items"])
    assert all("historical_sources" not in item for item in restored["items"])


class PracticeSources(PublicSources):
    def __init__(self):
        super().__init__()
        self.pages = {
            sid.removeprefix("docs:"): fixture["text"]
            for sid, fixture in PASSAGES.items()
            if sid.startswith("docs:")
        }

    def get(self, url):
        if "/skills/" in url and url.endswith("SKILL.md"):
            return Response(url, PASSAGES["skill:typesafe-ai"]["text"].encode(), {})
        return super().get(url)


@pytest.mark.parametrize("section", ["sources", "source_observations"])
@pytest.mark.parametrize("corruption", ["missing-source", "wrong-hash"])
def test_validator_rejects_practice_references_to_unretained_evidence(
    tmp_path, section, corruption
):
    synchronize(tmp_path, PracticeSources(), strict=True)
    path = tmp_path / "state/practices.json"
    practices = json.loads(path.read_text())
    if section == "sources":
        evidence = items(practices)["noul-is-yes-probability"]["sources"][0]
        if corruption == "missing-source":
            evidence["source_id"] = "docs:not-retained"
        else:
            evidence["sha256"] = "0" * 64
    else:
        source_id = "docs:primitives/noul"
        if corruption == "missing-source":
            practices["source_observations"]["docs:not-retained"] = practices[
                "source_observations"
            ].pop(source_id)
        else:
            practices["source_observations"][source_id] = "0" * 64
    path.write_text(json.dumps(practices))
    with pytest.raises(RuntimeError, match="practice .*does not match retained artifact"):
        validate_tree(tmp_path)


def test_validator_allows_unknown_historical_evidence_for_disappeared_sources(tmp_path):
    upstream = PracticeSources()
    synchronize(tmp_path, upstream, strict=True)
    del upstream.pages["primitives/noul"]
    synchronize(tmp_path, upstream, strict=True)
    practices = json.loads((tmp_path / "state/practices.json").read_text())
    current_sources = {
        artifact["source_id"]
        for artifact in json.loads((tmp_path / "state/sources.json").read_text())["artifacts"]
    }
    item = items(practices)["noul-is-yes-probability"]
    assert item["status"] == "unknown"
    assert item["sources"] == []
    assert item["historical_sources"][0]["source_id"] not in current_sources
    assert "docs:primitives/noul" not in practices["source_observations"]
    validate_tree(tmp_path, strict=True)


def test_repeated_sync_and_source_removal_preserve_evidence_and_history(tmp_path, monkeypatch):
    upstream = PracticeSources()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    original = snapshot(tmp_path)
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == original
    upstream.fail_page = True
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == original
    upstream.fail_page = False
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    upstream.pages["new-page"] = "# An unrelated addition"
    synchronize(tmp_path, upstream, strict=True)
    history = {
        name: body for name, body in snapshot(tmp_path).items() if name.startswith("events/")
    }
    prior = json.loads((tmp_path / "state/practices.json").read_text())
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-03T00:00:00Z")
    del upstream.pages["primitives/noul"]
    synchronize(tmp_path, upstream, strict=True)
    removed = json.loads((tmp_path / "state/practices.json").read_text())
    item = items(removed)["noul-is-yes-probability"]
    assert item["status"] == "unknown" and item["sources"] == []
    assert item["historical_sources"] == items(prior)[item["id"]]["sources"]
    assert all((tmp_path / name).read_bytes() == body for name, body in history.items())
    stable = snapshot(tmp_path)
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == stable


@pytest.mark.parametrize("sid", PASSAGES)
def test_fenced_unclosed_html_example_does_not_consume_later_reviewed_guidance(
    sid, review_contexts
):
    contents = {sid: "```html\n<code>\n```\n\n" + PASSAGES[sid]["text"]}
    review_contexts(
        contents, artifacts(contents), {sid: decisions(sid, PASSAGES[sid]["recommended"])}
    )
    state = derive_practices(contents, artifacts(contents), None)
    assert {item["id"] for item in state["items"] if item["status"] == "recommended"} == set(
        PASSAGES[sid]["recommended"]
    )


def test_excessive_nested_examples_do_not_yield_partial_recommendations():
    sid = "skill:typesafe-ai"
    depth = 80
    lines = ["  " * level + "- nested" for level in range(depth)]
    lines += ["  " * depth + "~~~mdx", "  " * depth + "<code>", "  " * depth + "~~~"]
    contents = {sid: "\n".join(lines) + "\n\n" + PASSAGES[sid]["text"]}
    state = derive_practices(contents, artifacts(contents), None)
    assert all(item["status"] == "unknown" for item in state["items"])


def test_real_sync_uses_retained_neutral_sources_for_tracking_migration(tmp_path, monkeypatch):
    rule = next(rule for rule in PRACTICE_RULES if rule["id"] == "deterministic-before-jev")
    neutral = "Use deterministic operations first."

    class LegacySources(PracticeSources):
        def __init__(self):
            super().__init__()
            for sid in rule["sources"]:
                if sid.startswith("docs:"):
                    self.pages[sid.removeprefix("docs:")] = neutral

        def get(self, url):
            if (
                "skill:typesafe-ai" in rule["sources"]
                and "/skills/" in url
                and url.endswith("SKILL.md")
            ):
                return Response(url, neutral.encode(), {})
            return super().get(url)

    upstream = LegacySources()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    path = tmp_path / "state/practices.json"
    legacy = json.loads(path.read_text())
    legacy.pop("evidence_policy")
    legacy.pop("source_observations")
    legacy.pop("fingerprint")
    supporting = rule["sources"][0]
    source = next(
        r
        for r in json.loads((tmp_path / "state/sources.json").read_text())["artifacts"]
        if r["source_id"] == supporting
    )
    item = items(legacy)[rule["id"]]
    item["status"] = "recommended"
    item["sources"] = [
        {
            "source_id": supporting,
            "url": source["fetch_url"],
            "sha256": source["sha256"],
            "excerpt": neutral,
        }
    ]
    legacy = observed_state(legacy, None)
    path.write_text(json.dumps(legacy))
    (tmp_path / "BEST_PRACTICES.md").write_text(render_best_practices(legacy))
    baseline = (tmp_path / "events/baseline.json").read_bytes()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    events = json.loads((tmp_path / "events/2026-10-02.json").read_text())["events"]
    event = next(e for e in events if e["category"] == "practice" and e["entity"] == rule["id"])
    assert event["change"] == "baseline"
    assert "tracking correction" in event["summary"]
    assert (tmp_path / "events/baseline.json").read_bytes() == baseline
    stable = snapshot(tmp_path)
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    assert snapshot(tmp_path) == stable


@pytest.mark.parametrize(
    "prefix",
    [
        "# Things to avoid\n\n",
        "# Avoid\n\n",
        "We advise the opposite of these rules.\n\n",
        '<Warning title="Deprecated practices">\n',
        "```text\nA new example also changes the complete context.\n```\n\n",
    ],
)
def test_changed_complete_context_cannot_recommend_unchanged_passages(prefix):
    sid = "skill:typesafe-ai"
    prior_contents = {sid: PASSAGES[sid]["text"]}
    prior = derive_practices(prior_contents, artifacts(prior_contents), None)
    contents = {sid: prefix + PASSAGES[sid]["text"]}
    current = derive_practices(contents, artifacts(contents), prior)
    for rid in PASSAGES[sid]["recommended"]:
        item = items(current)[rid]
        assert item["status"] == "unknown"
        assert item["historical_sources"] == items(prior)[rid]["sources"]
        assert any(row["verdict"] == "unreviewed" for row in item["evidence_reviews"])


@pytest.mark.parametrize("variant", ["line-endings", "trailing-whitespace"])
def test_equivalent_context_whitespace_preserves_recommendations(variant):
    sid = "skill:typesafe-ai"
    text = PASSAGES[sid]["text"]
    prior = derive_practices({sid: text}, artifacts({sid: text}), None)
    if variant == "line-endings":
        changed = text.replace("\n", "\r\n")
    else:
        changed = "\n".join(line + "  " for line in text.split("\n"))
    current = derive_practices({sid: changed}, artifacts({sid: changed}), prior)
    assert {i["id"] for i in current["items"] if i["status"] == "recommended"} == set(
        PASSAGES[sid]["recommended"]
    )
    assert semantic_events({"practices": prior}, {"practices": current}, "2026-10-02") == []


def test_explicit_review_of_changed_context_records_local_verification(
    review_contexts, monkeypatch
):
    sid = "skill:typesafe-ai"
    contents = {sid: "# Clarification\n\n" + PASSAGES[sid]["text"]}
    evidence = artifacts(contents)
    unknown = derive_practices(contents, evidence, None)
    assert all(i["status"] == "unknown" for i in unknown["items"])
    review_contexts(contents, evidence, {sid: decisions(sid, PASSAGES[sid]["recommended"])})
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    verified = derive_practices(contents, evidence, unknown)
    assert {i["id"] for i in verified["items"] if i["status"] == "recommended"} == set(
        PASSAGES[sid]["recommended"]
    )
    events = semantic_events({"practices": unknown}, {"practices": verified}, "2026-10-02")
    assert all(e["change"] == "verification_changed" for e in events)
    assert all("local context review" in e["summary"] for e in events)
    assert derive_practices(contents, evidence, verified) == verified


def test_reviewed_opposition_and_unreviewed_sources_block_other_support(review_contexts):
    rid = "deterministic-before-jev"
    supporting = "skill:typesafe-ai"
    other = "docs:concepts/how-to-build-with-system-one"
    contents = {supporting: PASSAGES[supporting]["text"], other: "A newly changed reference."}
    unknown = derive_practices(contents, artifacts(contents), None)
    assert items(unknown)[rid]["status"] == "unknown"
    review_contexts(
        {other: contents[other]}, artifacts({other: contents[other]}), {other: decisions(other)}
    )
    supported = derive_practices(contents, artifacts(contents), unknown)
    assert items(supported)[rid]["status"] == "recommended"
    rejected = {other: "# Things to avoid\n\n" + PASSAGES[supporting]["text"]}
    review_contexts(rejected, artifacts(rejected), {other: decisions(other, rejected=[rid])})
    contents.update(rejected)
    conflict = derive_practices(contents, artifacts(contents), supported)
    assert items(conflict)[rid]["status"] == "unknown"
    assert any(row["verdict"] == "rejected" for row in items(conflict)[rid]["evidence_reviews"])
    events = semantic_events({"practices": supported}, {"practices": conflict}, "2026-10-02")
    event = next(e for e in events if e["entity"] == rid)
    assert any(s["sha256"] == artifacts(rejected)[other].sha256 for s in event["sources"])


@pytest.mark.parametrize(
    "text",
    [
        "Failures have consequences; the state contains the content.",
        "```text\nKeep known rules, calculations, exact lookups, and execution in code.\n```",
    ],
)
def test_review_verdict_cannot_replace_complete_literal_passage_proof(review_contexts, text):
    sid = "skill:typesafe-ai"
    contents = {sid: text}
    evidence = artifacts(contents)
    rid = "deterministic-before-jev"
    review_contexts(contents, evidence, {sid: decisions(sid, supported=[rid])})
    state = derive_practices(contents, evidence, None)
    assert items(state)[rid]["status"] == "unknown"
    assert items(state)[rid]["sources"] == []
    assert any(r["verdict"] == "unverified_passages" for r in items(state)[rid]["evidence_reviews"])


def test_claim_fragments_from_two_sources_cannot_form_one_support_bundle(review_contexts):
    rule = next(r for r in PRACTICE_RULES if len(r["claims"]) > 1 and len(r["sources"]) > 1)
    blocks = list(reviewed_blocks(rule).values())
    first, second = rule["sources"][:2]
    contents = {first: blocks[0], second: "\n\n".join(blocks[1:])}
    evidence = artifacts(contents)
    review_contexts(
        contents, evidence, {sid: decisions(sid, supported=[rule["id"]]) for sid in contents}
    )
    state = derive_practices(contents, evidence, None)
    assert items(state)[rule["id"]]["status"] == "unknown"
    assert items(state)[rule["id"]]["sources"] == []


def test_missing_or_mismatched_artifact_never_establishes_supported_provenance():
    sid = "skill:typesafe-ai"
    contents = {sid: PASSAGES[sid]["text"]}
    other = artifacts({sid: "Different fetched bytes."})
    for evidence in ({}, other):
        state = derive_practices(contents, evidence, None)
        assert all(i["status"] == "unknown" and i["sources"] == [] for i in state["items"])


@pytest.mark.parametrize("neutral_observed", [True, False])
def test_migration_cannot_hide_changed_or_unproven_non_citing_source(neutral_observed):
    rid = "deterministic-before-jev"
    rule = next(r for r in PRACTICE_RULES if r["id"] == rid)
    contents = {sid: PASSAGES[sid]["text"] for sid in rule["sources"]}
    evidence = artifacts(contents)
    legacy = derive_practices(contents, evidence, None)
    assert items(legacy)[rid]["status"] == "recommended"
    legacy.pop("evidence_policy")
    legacy.pop("source_observations")
    non_citing = "docs:concepts/how-to-build-with-system-one"
    prior = {
        "practices": legacy,
        "sources": {
            "artifacts": [
                {"source_id": sid, "sha256": artifact.sha256}
                for sid, artifact in evidence.items()
                if neutral_observed or sid != non_citing
            ]
        },
    }
    contents[non_citing] = "# A completely changed context\n\n" + contents[non_citing]
    current = derive_practices(contents, artifacts(contents), legacy)
    event = next(
        e
        for e in semantic_events(prior, {"practices": current}, "2026-10-02")
        if e["entity"] == rid
    )
    assert event["change"] == "modified"
    assert "tracking correction" not in event["summary"]


@pytest.mark.parametrize("opening", ["<code>", '<code title="/>">'])
def test_reviewed_code_only_document_still_cannot_prove_recommendations(review_contexts, opening):
    sid = "skill:typesafe-ai"
    contents = {sid: opening + PASSAGES[sid]["text"] + "</code>"}
    evidence = artifacts(contents)
    review_contexts(contents, evidence, {sid: decisions(sid, PASSAGES[sid]["recommended"])})
    state = derive_practices(contents, evidence, None)
    assert all(item["status"] == "unknown" and item["sources"] == [] for item in state["items"])


@pytest.mark.parametrize("corruption", ["missing-review", "unreviewed", "wrong-context"])
def test_recommended_state_requires_matching_supported_context_provenance(tmp_path, corruption):
    synchronize(tmp_path, PracticeSources(), strict=True)
    path = tmp_path / "state/practices.json"
    state = json.loads(path.read_text())
    item = items(state)["deterministic-before-jev"]
    if corruption == "missing-review":
        item["evidence_reviews"] = []
    elif corruption == "unreviewed":
        item["evidence_reviews"][0]["verdict"] = "unreviewed"
    else:
        item["sources"][0]["context_sha256"] = "0" * 64
    path.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match="recommended practice lacks reviewed context"):
        validate_tree(tmp_path, strict=True)


def test_leading_indentation_changes_reviewed_prose_to_unreviewed_code():
    sid = "docs:concepts/how-to-build-with-system-one"
    text = PASSAGES[sid]["text"]
    supported = derive_practices({sid: text}, artifacts({sid: text}), None)
    assert items(supported)["narrow-coherent-questions"]["status"] == "recommended"
    indented = {sid: "    " + text}
    unknown = derive_practices(indented, artifacts(indented), supported)
    assert items(unknown)["narrow-coherent-questions"]["status"] == "unknown"
    assert any(
        row["verdict"] == "unreviewed"
        for row in items(unknown)["narrow-coherent-questions"]["evidence_reviews"]
    )


@pytest.mark.parametrize("verdict", ["rejected", "non_conflicting"])
def test_status_preserving_manual_context_reviews_are_recorded(review_contexts, verdict):
    sid = "skill:typesafe-ai"
    rid = "deterministic-before-jev"
    contents = {sid: "# A changed context\n\n" + PASSAGES[sid]["text"]}
    evidence = artifacts(contents)
    unknown = derive_practices(contents, evidence, None)
    assert items(unknown)[rid]["status"] == "unknown"
    scoped = decisions(sid, rejected=[rid]) if verdict == "rejected" else decisions(sid)
    review_contexts(contents, evidence, {sid: scoped})
    reviewed = derive_practices(contents, evidence, unknown)
    assert items(reviewed)[rid]["status"] == "unknown"
    events = semantic_events({"practices": unknown}, {"practices": reviewed}, "2026-10-02")
    event = next(e for e in events if e["entity"] == rid)
    assert event["change"] == "verification_changed"
    assert event["before_sha256"] != event["after_sha256"]
    assert "local context review" in event["summary"]
    assert semantic_events({"practices": reviewed}, {"practices": reviewed}, "2026-10-03") == []
