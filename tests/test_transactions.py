"""Exercise the same sync-then-validate sequence used by scheduled promotion."""

import json
from pathlib import Path

import pytest

from jev_docs.sync import FetchError, Response, synchronize, validate_tree


class PublicSources:
    def __init__(self):
        self.pages = {"page": "# A public page\n\nOriginal evidence.\n"}
        self.fail_index = False
        self.fail_page = False

    def get(self, url):
        if url.endswith("/llms.txt"):
            if self.fail_index:
                raise FetchError("index unavailable")
            body = "\n".join(f"- [{name}](https://docs.typesafe.ai/{name})" for name in self.pages)
        elif url.endswith("/sitemap.xml"):
            body = "<urlset><url><loc>https://docs.typesafe.ai/page</loc></url></urlset>"
        elif url.startswith("https://docs.typesafe.ai/"):
            if self.fail_page:
                raise FetchError("page unavailable")
            body = self.pages[url.split("typesafe.ai/")[1].removesuffix(".md")]
        elif "/skills/commits?" in url:
            body = [{"sha": "a" * 40}]
        elif "/skills/" in url and url.endswith("SKILL.md"):
            body = "# Skill\nKeep deterministic work in code."
        elif "/tags?" in url:
            body = [{"name": "v0.1.0", "commit": {"sha": "b" * 40}}]
        elif "/releases?" in url:
            body = [
                {
                    "tag_name": "v0.1.0",
                    "published_at": "2026-09-01",
                    "html_url": "https://github.com/typesafe-ai/example/releases/tag/v0.1.0",
                }
            ]
        elif "/commits/" in url:
            body = {"sha": "b" * 40}
        elif url.endswith("README.md") and "system-one-adapter-python" in url:
            body = "A drop-in replacement backed by LLM APIs.\n\nUseful for comparing TypeSafe against an LLM.\n\nInstall system-one-adapter[openai]."
        elif url.endswith("pyproject.toml"):
            body = 'version = "0.1.0"\nrequires-python = ">=3.10"\n'
        elif url.endswith("package.json"):
            body = {"version": "0.1.0"}
        elif "pypi.org" in url:
            body = {"info": {"version": "0.1.0"}, "releases": {"0.1.0": []}}
        elif "registry.npmjs.org" in url:
            body = {"dist-tags": {"latest": "0.1.0"}}
        elif "api.github.com/repos/" in url:
            body = {"default_branch": "main", "license": {"key": "mit"}}
        else:
            raise AssertionError(f"unhandled fixture URL {url}")
        return Response(url, (body if isinstance(body, str) else json.dumps(body)).encode(), {})


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_scheduled_upstream_addition_transaction(tmp_path: Path):
    upstream = PublicSources()
    assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
    baseline = snapshot(tmp_path)
    stable = synchronize(tmp_path, upstream, strict=True)
    assert stable["newly_discovered"] == []
    assert stable["events"] == 0
    assert snapshot(tmp_path) == baseline

    # Arbitrary future additions, not special cases for the September 26 pages.
    upstream.pages.update({"future/one": "# One", "future/two": "# Two"})
    added = synchronize(tmp_path, upstream, strict=True)
    assert added["newly_discovered"] == ["docs:future/one", "docs:future/two"]
    assert added["events"] == 2
    validate_tree(tmp_path, strict=True)
    events = json.loads((tmp_path / f"events/{added['day']}.json").read_text())["events"]
    assert {(event["entity"], event["change"]) for event in events} == {
        ("docs:future/one", "added"),
        ("docs:future/two", "added"),
    }
    promoted = snapshot(tmp_path)
    stable = synchronize(tmp_path, upstream, strict=True)
    assert stable["newly_discovered"] == []
    assert stable["events"] == 0
    assert snapshot(tmp_path) == promoted

    # Even a working sitemap cannot turn failed canonical discovery into removals.
    upstream.fail_index = True
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == promoted
    upstream.fail_index = False
    upstream.fail_page = True
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, upstream, strict=True)
    assert snapshot(tmp_path) == promoted


def test_failed_derivation_keeps_tree_and_reports_candidate_discovery(tmp_path):
    upstream = PublicSources()
    synchronize(tmp_path, upstream, strict=True)
    baseline = snapshot(tmp_path)
    upstream.pages["cookbooks"] = "# Changed index format without recognizable rows"
    attempt = {}
    with pytest.raises(RuntimeError, match="malformed cookbook"):
        synchronize(tmp_path, upstream, strict=True, attempt=attempt)
    assert snapshot(tmp_path) == baseline
    assert attempt["phase"] == "derivation"
    assert attempt["newly_discovered"] == ["docs:cookbooks"]
    assert attempt["candidate_sdk_versions"] == {"python": "0.1.0", "javascript": "0.1.0"}


def test_cli_attempt_diagnostics_never_expose_exception_body(tmp_path, monkeypatch):
    from jev_docs import sync

    diagnostic = tmp_path / "attempt.json"

    def fail(*args, **kwargs):
        kwargs["attempt"]["phase"] = "fetch"
        raise RuntimeError("private response body")

    monkeypatch.setattr(sync, "synchronize", fail)
    assert sync.main(["sync", "--diagnostics", str(diagnostic)]) == 1
    report = json.loads(diagnostic.read_text())
    assert report["result"] == "failure" and report["phase"] == "fetch"
    assert "private" not in diagnostic.read_text()
    monkeypatch.setattr(sync, "synchronize", lambda *args, **kwargs: {"events": 0})
    assert sync.main(["sync", "--diagnostics", str(diagnostic)]) == 0
    assert json.loads(diagnostic.read_text())["result"] == "success"
