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


class UntaggedPythonRelease(PublicSources):
    """PyPI publishes a version that has no matching Git tag or GitHub release."""

    def get(self, url):
        if "pypi.org" in url:
            body = {"info": {"version": "0.2.0"}, "releases": {"0.1.0": [], "0.2.0": []}}
            return Response(url, json.dumps(body).encode(), {})
        return super().get(url)


def test_unrelated_changes_do_not_restart_sdk_discrepancy_grace_period(tmp_path, monkeypatch):
    upstream = UntaggedPythonRelease()
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    assert json.loads((tmp_path / "state/freshness.json").read_text())["python"]["status"] == (
        "discrepancy"
    )

    # A docs change inside the grace period changes the freshness snapshot but must
    # not restart the discrepancy's grace period.
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-02T00:00:00Z")
    upstream.pages["another"] = "# Another page"
    synchronize(tmp_path, upstream, strict=True)

    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-05T00:00:00Z")
    upstream.pages["third"] = "# Third page"
    sdk_before = (tmp_path / "state/sdk-python.json").read_bytes()
    with pytest.raises(RuntimeError, match="beyond grace period: python"):
        synchronize(tmp_path, upstream, strict=True)
    # Expired health still fails, but independent valid docs are now publishable.
    assert (tmp_path / "state/sdk-python.json").read_bytes() == sdk_before
    assert "docs:third" in {
        page["source_id"]
        for page in json.loads((tmp_path / "state/source-coverage.json").read_text())["pages"]
    }
    validate_tree(tmp_path, strict=True)
    freshness = json.loads((tmp_path / "state/freshness.json").read_text())
    assert freshness["observed_at"] == "2026-10-05T00:00:00Z"
    assert freshness["python"]["discrepancy_first_observed"] == "2026-10-01T00:00:00Z"

    # Convergence clears the discrepancy; a later recurrence starts a new period.
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-06T00:00:00Z")
    synchronize(tmp_path, PublicSources(), strict=True)
    assert (
        "discrepancy_first_observed"
        not in json.loads((tmp_path / "state/freshness.json").read_text())["python"]
    )
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-07T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    assert (
        json.loads((tmp_path / "state/freshness.json").read_text())["python"][
            "discrepancy_first_observed"
        ]
        == "2026-10-07T00:00:00Z"
    )


class MarkdownFailure(PublicSources):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def get(self, url):
        if url == "https://docs.typesafe.ai/page.md":
            raise FetchError("markdown unavailable", self.status)
        if url == "https://docs.typesafe.ai/page":
            return Response(url, b"<html>rendered page</html>", {})
        return super().get(url)


def test_transient_markdown_failure_does_not_substitute_html(tmp_path):
    synchronize(tmp_path, PublicSources(), strict=True)
    promoted = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="last-known-good"):
        synchronize(tmp_path, MarkdownFailure(503), strict=True)
    assert snapshot(tmp_path) == promoted


def test_missing_markdown_representation_uses_page_form(tmp_path):
    synchronize(tmp_path, MarkdownFailure(404), strict=True)
    manifest = json.loads((tmp_path / "sources/docs.typesafe.ai/manifest.json").read_text())
    page = next(item for item in manifest["artifacts"] if item["source_id"] == "docs:page")
    assert page["fetch_url"] == "https://docs.typesafe.ai/page"


class RedeployedSitemap(PublicSources):
    def __init__(self):
        super().__init__()
        self.lastmod = "2026-10-01T00:00:00Z"

    def get(self, url):
        if url.endswith("/sitemap.xml"):
            lastmod = f"<lastmod>{self.lastmod}</lastmod>" if self.lastmod else ""
            body = f"<urlset><url><loc>https://docs.typesafe.ai/page</loc>{lastmod}</url></urlset>"
            return Response(url, body.encode(), {})
        return super().get(url)


def test_sitemap_lastmod_alone_does_not_churn_and_manifests_agree(tmp_path, monkeypatch):
    upstream = RedeployedSitemap()
    upstream.lastmod = None
    monkeypatch.setenv("JEVDOCS_OBSERVED_AT", "2026-10-01T00:00:00Z")
    synchronize(tmp_path, upstream, strict=True)
    promoted = snapshot(tmp_path)

    # Sitemap metadata appearing, or a redeploy bumping it, without a content change.
    for day, lastmod in (("02", "2026-10-02T00:00:00Z"), ("03", "2026-10-03T00:00:00Z")):
        monkeypatch.setenv("JEVDOCS_OBSERVED_AT", f"2026-10-{day}T00:00:00Z")
        upstream.lastmod = lastmod
        assert synchronize(tmp_path, upstream, strict=True)["events"] == 0
        assert snapshot(tmp_path) == promoted

    # A content change records the lastmod observed with the new content.
    upstream.pages["page"] = "# A public page\n\nRevised evidence.\n"
    synchronize(tmp_path, upstream, strict=True)
    docs = json.loads((tmp_path / "sources/docs.typesafe.ai/manifest.json").read_text())
    github = json.loads((tmp_path / "sources/github.typesafe-ai/manifest.json").read_text())
    sources = json.loads((tmp_path / "state/sources.json").read_text())
    page = next(item for item in docs["artifacts"] if item["source_id"] == "docs:page")
    assert page["upstream_last_modified"] == "2026-10-03T00:00:00Z"
    assert page["observed_at"] == "2026-10-03T00:00:00Z"
    assert sources["artifacts"] == sorted(
        docs["artifacts"] + github["artifacts"], key=lambda item: item["source_id"]
    )


def test_strict_validation_checks_history_and_renderings(tmp_path):
    upstream = PublicSources()
    synchronize(tmp_path, upstream, strict=True)
    upstream.pages["future"] = "# Future"
    day = synchronize(tmp_path, upstream, strict=True)["day"]
    validate_tree(tmp_path, strict=True)

    report = tmp_path / f"changes/{day}.md"
    original_report = report.read_text()
    report.write_text(original_report + "- hand-edited line\n")
    validate_tree(tmp_path)
    with pytest.raises(RuntimeError, match="daily report disagrees"):
        validate_tree(tmp_path, strict=True)
    report.write_text(original_report)

    events = json.loads((tmp_path / f"events/{day}.json").read_text())
    copy = tmp_path / "events/2000-01-01.json"
    copy.write_text(json.dumps({**events, "date": "2000-01-01"}))
    (tmp_path / "changes/2000-01-01.md").write_text(original_report.replace(day, "2000-01-01"))
    validate_tree(tmp_path)
    with pytest.raises(RuntimeError, match="recorded in"):
        validate_tree(tmp_path, strict=True)
    copy.unlink()

    # The 2000-01-01 report now has no event file behind it.
    validate_tree(tmp_path)
    with pytest.raises(RuntimeError, match="without event files: 2000-01-01.md"):
        validate_tree(tmp_path, strict=True)
    (tmp_path / "changes/2000-01-01.md").unlink()
    validate_tree(tmp_path, strict=True)

    guide = tmp_path / "BEST_PRACTICES.md"
    guide.write_text(guide.read_text() + "\nUnsupported claim.\n")
    validate_tree(tmp_path)
    with pytest.raises(RuntimeError, match="best-practices rendering"):
        validate_tree(tmp_path, strict=True)


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
