import pytest

from jev_docs.inventories import cookbooks, model_limitations
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


def test_agent_practice_identity_survives_presentation_and_absence_is_unknown():
    sid = "docs:introduction/coding-agents"
    text = 'Jev is **not** a drop-in replacement for a coding agent.\n\n* Replace a fragile prompt asking for "return JSON" with typed answers.'
    old = derive_practices({sid: text}, evidence(sid, text), None)
    wrapped = "<Note>\n" + text + "\n</Note>"
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
