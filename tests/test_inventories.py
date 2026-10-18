import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from jev_docs import inventories
from jev_docs.inventories import cookbooks, model_limitations, render_limitations
from jev_docs.sync import Artifact, derive_practices, semantic_events
from jev_docs.text import excerpt


def evidence(sid, text):
    return {
        sid: Artifact(
            sid, "documentation", "https://docs.typesafe.ai/" + sid[5:] + ".md", text.encode()
        )
    }


def limitations(model="jev-1.13", mitigation="Count in code."):
    return f"Applies to `{model}`. Last reviewed 2026-09-17.\n\n## Counting\n\nThe model cannot count reliably.\n\n**Instead:** {mitigation}\n"


def test_model_applicability_identity_mitigation_and_conservative_absence():
    sid = "docs:model-jaggedness/jev-1.13"
    text = limitations()
    old = model_limitations({sid: text}, evidence(sid, text), None)
    assert old["items"][0]["id"] == "jev-1.13:counting"
    changed = limitations(mitigation="Parse and count in code.")
    new = model_limitations({sid: changed}, evidence(sid, changed), old)
    events = semantic_events({"model-limitations": old}, {"model-limitations": new}, "today")
    assert [(e["category"], e["change"]) for e in events] == [("model_limitation", "modified")]
    absent = model_limitations({}, {}, old)
    assert absent["items"][0]["status"] == "unknown"
    assert absent["items"][0]["provenance"] == old["items"][0]["provenance"]
    later_sid = "docs:model-jaggedness/jev-1.14"
    later = limitations("jev-1.14", "This version has different guidance.")
    both = model_limitations(
        {sid: text, later_sid: later}, evidence(sid, text) | evidence(later_sid, later), old
    )
    assert {item["model"] for item in both["items"]} == {"jev-1.13", "jev-1.14"}
    assert both["items"][0] == old["items"][0]
    with pytest.raises(RuntimeError, match="applicability"):
        model_limitations({sid: later}, evidence(sid, later), old)
    with pytest.raises(RuntimeError, match="empty limitations"):
        model_limitations({sid: text.split("##")[0]}, evidence(sid, text), old)


def test_limitations_unknown_sections_do_not_copy_or_invent_guidance():
    sid = "docs:model-jaggedness/jev-1.13"
    text = limitations(mitigation="Private-looking upstream prose. " * 100)
    state = model_limitations({sid: text}, evidence(sid, text), None)
    item = state["items"][0]
    assert item["summary_kind"] == "unknown"
    assert item["summary"].startswith("Unknown:")
    assert len(item["summary"]) <= 160
    assert item["mitigation"] is None
    assert item["provenance"] == [
        {
            "source_id": sid,
            "url": evidence(sid, text)[sid].url + "#counting",
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    ]
    assert "Private-looking" not in render_limitations(state)
    assert model_limitations({sid: text}, evidence(sid, text), state) == state
    novel = text.replace("## Counting", "## Never reviewed")
    novel_state = model_limitations({sid: novel}, evidence(sid, novel), None)
    assert novel_state["items"][0]["summary_kind"] == "unknown"


def test_recorded_limitations_interpretations_and_legacy_rendering():
    recorded = json.loads((Path(__file__).parents[1] / "state/model-limitations.json").read_text())
    original = copy.deepcopy(recorded)
    retained = model_limitations({}, {}, recorded)
    by_section = {item["section"]: item for item in retained["items"]}
    assert all(item["status"] == "unknown" for item in retained["items"])
    assert all(item["mitigation"] is None for item in retained["items"])
    assert all(item["summary_kind"] == "repository-interpretation" for item in retained["items"])
    assert all(len(item["summary"]) <= 200 for item in retained["items"])
    assert "aggregate programmatically" in by_section["counting"]["summary"]
    assert "not an exact numerical measurement" in by_section["math-using-score"]["summary"]
    assert "separately" in by_section["common-sense-structural-invariants"]["summary"]
    for before, after in zip(recorded["items"], retained["items"], strict=True):
        for field in ("id", "model", "section_text_sha256", "provenance", "last_reviewed"):
            assert before[field] == after[field]
    assert (
        render_limitations(recorded)
        == (Path(__file__).parents[1] / "MODEL_LIMITATIONS.md").read_text()
    )
    rendered = render_limitations(retained)
    assert rendered == render_limitations(retained)
    assert "Repository interpretation" in rendered
    assert "Upstream mitigation:" not in rendered
    assert all(item["summary"] not in rendered for item in recorded["items"])
    assert recorded == original
    later = copy.deepcopy(retained)
    later["items"][0]["model"] = "jev-1.14"
    assert "Repository interpretation (unknown): Unknown:" in render_limitations(later)
    changed = copy.deepcopy(retained)
    changed["items"][0]["section_text_sha256"] = "0" * 64
    assert "Repository interpretation (unknown): Unknown:" in render_limitations(changed)


def test_limitation_quotation_policy_migration_is_local_baseline():
    recorded = json.loads((Path(__file__).parents[1] / "state/model-limitations.json").read_text())
    upgraded = model_limitations({}, {}, recorded)
    # Isolate the local format migration from absence/status transitions.
    for before, after in zip(recorded["items"], upgraded["items"], strict=True):
        after["status"] = before["status"]
        assert after["section_text_sha256"] == before["section_text_sha256"]
    old = {"model-limitations": recorded}
    new = {"model-limitations": upgraded}
    events = semantic_events(old, new, "today")
    assert len(events) == len(recorded["items"])
    assert all(event["change"] == "baseline" for event in events)
    assert all("local interpretation and quotation policy" in event["summary"] for event in events)
    assert all("not an upstream limitation change" in event["summary"] for event in events)
    assert semantic_events(old, new, "tomorrow") == [
        dict(event, observed_at="tomorrow") for event in events
    ]
    assert semantic_events(new, new, "tomorrow") == []
    changed = copy.deepcopy(new)
    changed["model-limitations"]["items"][0]["section_text_sha256"] = "0" * 64
    assert semantic_events(new, changed, "tomorrow")[0]["change"] == "modified"
    changed = copy.deepcopy(new)
    changed["model-limitations"]["items"][0]["status"] = "unknown"
    assert semantic_events(new, changed, "tomorrow")[0]["change"] == "modified"
    for field, value in (
        ("section_text_sha256", "0" * 64),
        ("status", "unknown"),
        ("model", "jev-1.14"),
        ("id", "jev-1.14:adversarial-content"),
    ):
        concurrent = copy.deepcopy(new)
        concurrent["model-limitations"]["items"][0][field] = value
        concurrent_events = semantic_events(old, concurrent, "today")
        if field == "id":
            # An identity change is separate removed/added tracking, not migration.
            assert {event["change"] for event in concurrent_events} >= {"removed", "added"}
            continue
        event = concurrent_events[0]
        assert event["change"] == "modified"
        assert "evidence or status also changed" in event["summary"]
        assert "local quotation-policy migration" in event["summary"]
        assert "not an upstream limitation change" not in event["summary"]
        assert semantic_events(concurrent, concurrent, "tomorrow") == []


@pytest.mark.parametrize(
    "suffix", ["```python\npass\n```", "<Info>Example</Info>", "<Tip>Example</Tip>"]
)
def test_complete_section_hash_includes_examples_and_callouts(monkeypatch, suffix):
    sid = "docs:model-jaggedness/jev-1.13"
    text = limitations() + suffix
    body = text.split("## Counting", 1)[1]
    complete_hash = hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode()).hexdigest()
    monkeypatch.setitem(inventories._COMPLETE_LIMITATION_HASHES, "counting", complete_hash)
    old = model_limitations({sid: text}, evidence(sid, text), None)
    assert old["items"][0]["complete_section_sha256"] == complete_hash
    assert old["items"][0]["summary_kind"] == "repository-interpretation"
    changed = text + "\nThis section now requires additional review."
    new = model_limitations({sid: changed}, evidence(sid, changed), old)
    assert new["items"][0]["section_text_sha256"] == old["items"][0]["section_text_sha256"]
    assert new["items"][0]["complete_section_sha256"] != complete_hash
    assert new["items"][0]["summary_kind"] == "unknown"
    assert "Repository interpretation (unknown)" in render_limitations(new)
    events = semantic_events({"model-limitations": old}, {"model-limitations": new}, "today")
    assert len(events) == 1 and events[0]["change"] == "modified"
    legacy = copy.deepcopy(old)
    del legacy["items"][0]["complete_section_sha256"]
    baseline = semantic_events({"model-limitations": legacy}, {"model-limitations": old}, "today")
    assert baseline[0]["change"] == "baseline"
    concurrent = semantic_events({"model-limitations": legacy}, {"model-limitations": new}, "today")
    assert concurrent[0]["change"] == "modified"
    assert "evidence or status also changed" in concurrent[0]["summary"]


INDEX = "## Extraction\n\n| [Dates](/cookbooks/dates) | Extract date parts. | Beginner |\n"


def test_cookbook_category_level_and_independent_discovery():
    contents = {"docs:cookbooks": INDEX, "docs:cookbooks/early": "# Early"}
    artifacts = evidence("docs:cookbooks", INDEX) | evidence("docs:cookbooks/early", "# Early")
    state = cookbooks(
        contents,
        artifacts,
        [("https://docs.typesafe.ai/cookbooks/early", "Early", "Not indexed yet.")],
        None,
    )
    dates, early = state["items"]
    assert (dates["id"], dates["category"], dates["level"]) == (
        "cookbooks/dates",
        "extraction",
        "Beginner",
    )
    assert dates["indexed"] and not dates["discovered"]
    assert early["discovered"] and not early["indexed"] and early["level"] is None
    changed = cookbooks(
        {"docs:cookbooks": INDEX.replace("Beginner", "Advanced")},
        evidence("docs:cookbooks", INDEX),
        [],
        state,
    )
    events = semantic_events({"cookbooks": state}, {"cookbooks": changed}, "today")
    assert {(e["entity"], e["change"]) for e in events} == {
        ("cookbooks/dates", "modified"),
        ("cookbooks/early", "removed"),
    }
    added = semantic_events({"cookbooks": {"items": []}}, {"cookbooks": state}, "today")
    assert all(e["change"] == "added" for e in added)
    assert semantic_events({"cookbooks": state}, {"cookbooks": state}, "tomorrow") == []


@pytest.mark.parametrize(
    "text",
    [
        "# Changed format",
        INDEX.replace("Beginner", "Unrecognized"),
        INDEX.replace("| Beginner |", "|"),
        INDEX + INDEX,
    ],
)
def test_malformed_cookbook_index_fails_conservatively(text):
    with pytest.raises(RuntimeError):
        cookbooks({"docs:cookbooks": text}, evidence("docs:cookbooks", text), [], None)


def test_new_layer_establishes_baseline_not_historical_additions():
    events = semantic_events(
        {}, {"model-limitations": {"items": [{"id": "jev-1.13:counting"}]}}, "today"
    )
    assert len(events) == 1 and events[0]["change"] == "baseline"


def test_agent_practice_identity_survives_presentation_and_absence_is_unknown(review_contexts):
    sid = "docs:introduction/coding-agents"
    fixture = json.loads((Path(__file__).parent / "fixtures/practice-passages.json").read_text())
    text = fixture[sid]["text"]
    verdicts = {sid: {rule_id: "supported" for rule_id in fixture[sid]["recommended"]}}
    review_contexts({sid: text}, evidence(sid, text), verdicts)
    old = derive_practices({sid: text}, evidence(sid, text), None)
    wrapped = "<Note>\n" + text + "\n</Note>"
    unreviewed = derive_practices({sid: wrapped}, evidence(sid, wrapped), old)
    assert all(
        item["status"] == "unknown"
        for item in unreviewed["items"]
        if item["category"] == "agent-integration"
    )
    review_contexts({sid: wrapped}, evidence(sid, wrapped), verdicts)
    new = derive_practices({sid: wrapped}, evidence(sid, wrapped), old)
    agent_items = [i for i in new["items"] if i["category"] == "agent-integration"]
    assert len(agent_items) == 2 and all(i["status"] == "recommended" for i in agent_items)
    assert semantic_events({"practices": old}, {"practices": new}, "today") == []
    absent = derive_practices({}, {}, old)
    assert all(i["status"] == "unknown" for i in absent["items"])


def test_excerpt_boundaries_cleanliness_meaning_and_size():
    text = "<Note>\nThe first sentence wraps\nonto another line. The target uses x < 3 and y > 1, never equality.\n</Note>"
    result = excerpt(text, "target", 70)
    assert result == "The target uses x < 3 and y > 1, never equality."
    assert excerpt(text, "target", 70) == result
    assert (
        excerpt("* A list item with `target` and **meaning**.", "target")
        == "A list item with target and meaning."
    )
    long = "Target " + "word " * 100
    bounded = excerpt(long, "Target", 50)
    assert len(bounded) <= 50 and bounded.endswith("word …")
    assert excerpt("Do not " + "word " * 100 + "target", "target", 50) is None


def test_excerpt_from_jsx_prose_preserves_math_and_avoids_demo_state():
    text = '<div>{selected ? "Selected" : other}</div><details><summary>Demo</summary><p>The target computes x * y and x < 3.</p></details>'
    assert excerpt(text, "target") == "The target computes x * y and x < 3."


def test_new_evaluation_layer_baseline_id_ignores_observation_time():
    state = {
        "version": "0.2.1",
        "source_commit": "a" * 40,
        "observed_at": "today",
        "provenance": [],
    }
    first = semantic_events({}, {"evaluation-tool": state}, "today")[0]
    second = semantic_events(
        {}, {"evaluation-tool": {**state, "observed_at": "tomorrow"}}, "tomorrow"
    )[0]
    assert first["change"] == "baseline" and first["id"] == second["id"]
