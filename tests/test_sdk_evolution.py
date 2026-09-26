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


def test_release_configuration_mismatch_fails():
    import pytest

    release = {"version": "0.7.1", "tag": "v0.7.1", "source_commit": "a" * 40}
    notes = [{"version": "0.7.1", "notes": ["add http2 extra"]}]
    with pytest.raises(RuntimeError, match="release config"):
        python_capabilities(
            SDKSource(),
            {},
            "typesafe-ai/typesafe-sdk-python",
            [release],
            {"releases": {"0.7.1": []}},
            notes,
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
