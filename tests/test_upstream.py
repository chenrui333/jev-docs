"""Bound public evidence requests and reject malformed metadata before promotion."""

import json
import traceback
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.response import addinfourl

import pytest
from test_transactions import PublicSources, snapshot

from jev_docs.sync import (
    FetchError,
    HTTPClient,
    Response,
    evaluation_tool_snapshot,
    github_json,
    parse_llms,
    parse_llms_exclusions,
    parse_sitemap,
    registry_json,
    sdk_snapshot,
    sha256_json,
    stable_release_evidence,
    stable_tag_evidence,
    synchronize,
)

SECRET_MARKER = "-".join(("private", "token"))


class Raw:
    status = 200
    headers = {}

    def __init__(self, url, body=b"ok"):
        self.url, self.body = url, body
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit):
        self.read_limits.append(limit)
        return self.body[:limit]


def test_timeout_budget_and_errors_do_not_disclose_exception_details():
    calls, delays = [], []

    def opener(request, timeout):
        calls.append(timeout)
        raise TimeoutError("private-token upstream response body")

    with pytest.raises(FetchError) as caught:
        HTTPClient(opener=opener, sleep=delays.append, timeout=3).get(
            "https://api.github.com/repos/o/r"
        )
    assert calls == [3, 3, 3]
    assert delays == [0.25, 0.5]
    assert "private-token" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.code == "fetch_failed"


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({}, [0.25, 0.5]),
        ({"Retry-After": "999999999999999999999"}, [60, 60]),
        ({"Retry-After": "garbage"}, [0.25, 0.5]),
        ({"Retry-After": "-1"}, [0.25, 0.5]),
        ({"Retry-After": "nan"}, [0.25, 0.5]),
        ({"Retry-After": "inf"}, [0.25, 0.5]),
        ({"Retry-After": "2"}, [2, 2]),
        ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999999999999"}, [60, 60]),
    ],
)
def test_429_has_bounded_waits_and_exact_retry_budget(headers, expected):
    calls, delays = [], []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 429, "private-token", headers, None)

    with pytest.raises(FetchError) as caught:
        HTTPClient(opener=opener, sleep=delays.append).get("https://api.github.com/repos/o/r")
    assert len(calls) == 3
    assert delays == expected
    assert caught.value.status == 429
    assert "private-token" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "url,headers,attempts,expected",
    [
        ("https://api.github.com/repos/o/r", {}, 1, []),
        ("https://api.github.com/repos/o/r", {"X-RateLimit-Remaining": "1"}, 1, []),
        ("https://api.github.com/repos/o/r", {"Retry-After": "bad"}, 1, []),
        ("https://api.github.com/repos/o/r", {"Retry-After": "5"}, 3, [5, 5]),
        ("https://api.github.com/repos/o/r", {"X-RateLimit-Remaining": "0"}, 3, [0.25, 0.5]),
        (
            "https://api.github.com/repos/o/r",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999999999999"},
            3,
            [60, 60],
        ),
        ("https://pypi.org/pypi/pkg/json", {"X-RateLimit-Remaining": "0"}, 1, []),
    ],
)
def test_only_explicit_github_rate_limit_403_is_retried(url, headers, attempts, expected):
    calls, delays = [], []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 403, "forbidden", headers, None)

    with pytest.raises(FetchError) as caught:
        HTTPClient(opener=opener, sleep=delays.append).get(url)
    assert caught.value.status == 403
    assert len(calls) == attempts
    assert delays == expected


def test_retry_after_http_date_is_respected_and_bounded(monkeypatch):
    monkeypatch.setattr("jev_docs.sync.time.time", lambda: 0)
    delays = []
    date = format_datetime(datetime(1970, 1, 1, 0, 0, 7, tzinfo=timezone.utc), usegmt=True)

    def opener(request, timeout):
        raise HTTPError(request.full_url, 429, "limited", {"Retry-After": date}, None)

    with pytest.raises(FetchError):
        HTTPClient(opener=opener, sleep=delays.append).get("https://api.github.com/repos/o/r")
    assert delays == [7, 7]


def test_response_bytes_are_bounded_before_reading():
    raw = Raw("https://example.test", b"12345")
    client = HTTPClient(opener=lambda request, timeout: raw, max_response_bytes=4)
    with pytest.raises(FetchError, match="response exceeds") as caught:
        client.get(raw.url)
    assert caught.value.code == "response_too_large"
    assert raw.read_limits == [5]
    raw = Raw(raw.url, b"1234")
    assert (
        HTTPClient(opener=lambda request, timeout: raw, max_response_bytes=4).get(raw.url).body
        == b"1234"
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_response_limit_cannot_disable_bounded_reads(limit):
    with pytest.raises(ValueError, match="positive"):
        HTTPClient(max_response_bytes=limit)


def test_invalid_authorization_header_never_appears_in_traceback(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token\ninvalid")

    def opener(request, timeout):
        raise ValueError(f"Invalid header value {request.get_header('Authorization')!r}")

    with pytest.raises(FetchError) as caught:
        HTTPClient(opener=opener).get("https://api.github.com/repos/o/r")
    assert "private-token" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "url,authenticated",
    [
        ("https://api.github.com/repos/o/r", True),
        ("https://api.github.com:443/repos/o/r", True),
        ("https://raw.githubusercontent.com/o/r/main/file", False),
        ("https://pypi.org/pypi/pkg/json", False),
        ("https://registry.npmjs.org/pkg", False),
        ("https://api.github.com.evil.test/repos/o/r", False),
        ("https://api.github.com:444/repos/o/r", False),
    ],
)
def test_token_is_scoped_to_exact_github_api_origin(monkeypatch, url, authenticated):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return Raw(request.full_url)

    HTTPClient(opener=opener).get(url)
    assert requests[0].get_header("Authorization") == (
        "Bearer private-token" if authenticated else None
    )


def test_github_authentication_is_optional(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def opener(request, timeout):
        assert request.get_header("Authorization") is None
        return Raw(request.full_url)

    assert HTTPClient(opener=opener).get("https://api.github.com/repos/o/r").body == b"ok"


def test_redirect_guard_runs_before_follow_and_never_forwards_authorization(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")
    client = HTTPClient()
    handler = next(h for h in client.opener.__self__.handlers if isinstance(h, HTTPRedirectHandler))
    request = Request(
        "https://api.github.com/repos/o/r", headers={"Authorization": "Bearer private-token"}
    )
    for target in (
        "https://elsewhere.test/steal",
        "http://api.github.com/repos/o/r",
        "https://user:private-token@api.github.com/repos/o/r",
    ):
        with pytest.raises(FetchError, match="cross-origin redirect") as caught:
            handler.redirect_request(request, None, 302, "redirect", {}, target)
        assert "private-token" not in str(caught.value)
    moved = handler.redirect_request(
        request, None, 302, "redirect", {}, "https://api.github.com/repos/o/r/"
    )
    assert moved.get_header("Authorization") is None


@pytest.mark.parametrize(
    "target", ["https://elsewhere.test/private", "https://api.github.com/moved"]
)
def test_urllib_redirect_transport_never_contacts_foreign_origin(monkeypatch, target):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")
    client = HTTPClient()
    guard = next(h for h in client.opener.__self__.handlers if isinstance(h, HTTPRedirectHandler))
    calls = []

    class Transport(HTTPSHandler):
        def https_open(self, request):
            calls.append((request.full_url, request.get_header("Authorization")))
            headers = Message()
            status = 200
            if len(calls) == 1:
                headers["Location"] = target
                status = 302
            response = addinfourl(BytesIO(b"ok"), headers, request.full_url, status)
            response.msg = "Found" if status == 302 else "OK"
            return response

    client.opener = build_opener(guard, Transport()).open
    initial = "https://api.github.com/repos/o/r"
    if "elsewhere" in target:
        with pytest.raises(FetchError, match="cross-origin redirect"):
            client.get(initial)
        assert calls == [(initial, "Bearer private-token")]
    else:
        assert client.get(initial).body == b"ok"
        assert calls == [(initial, "Bearer private-token"), (target, None)]


@pytest.mark.parametrize(
    "url",
    ["http://api.github.com/repos/o/r", "https://user:private-token@api.github.com/repos/o/r"],
)
def test_unsafe_request_url_is_rejected_before_open(url):
    def opener(request, timeout):
        pytest.fail("unsafe URL reached opener")

    with pytest.raises(FetchError) as caught:
        HTTPClient(opener=opener).get(url)
    assert "private-token" not in str(caught.value)


def test_non_https_discovery_links_are_excluded():
    text = "- [Unsafe](http://docs.typesafe.ai/page)\n- [Safe](https://docs.typesafe.ai/page)"
    assert parse_llms(text) == [("https://docs.typesafe.ai/page", "Safe", "")]
    assert parse_llms_exclusions(text) == [
        {"url": "http://docs.typesafe.ai/page", "title": "Unsafe", "reason": "non-https"}
    ]
    assert (
        parse_sitemap("<urlset><url><loc>http://docs.typesafe.ai/page</loc></url></urlset>") == {}
    )


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        value, headers = self.pages[url]
        return Response(
            url, value if isinstance(value, bytes) else json.dumps(value).encode(), headers
        )


@pytest.mark.parametrize(
    "surface,entries,projection",
    [
        (
            "tags",
            [
                {"name": "v0.1.0", "commit": {"sha": "a"}},
                {"name": "v0.2.0", "commit": {"sha": "b"}},
            ],
            stable_tag_evidence,
        ),
        ("releases", [{"tag_name": "v0.1.0"}, {"tag_name": "v0.2.0"}], stable_release_evidence),
    ],
)
def test_pagination_two_pages_deduplicates_and_stabilizes_evidence(surface, entries, projection):
    path = f"/repos/o/r/{surface}?per_page=100"
    first, second = (
        "https://api.github.com" + path,
        f"https://api.github.com/repos/o/r/{surface}?page=2",
    )
    client = Pages(
        {
            first: ([entries[1]], {"link": f'<{second}>; rel="next"'}),
            second: ([{**entries[1], "volatile": 2}, entries[0]], {}),
        }
    )
    values, response = github_json(client, path)
    assert client.calls == [first, second]
    assert response.url == first
    assert len(values) == 2
    single, _ = github_json(Pages({first: (list(reversed(entries)), {})}), path)
    assert values == single or projection(values) == projection(single)
    assert projection(values) == projection(entries)
    assert sha256_json(projection(values)) == sha256_json(projection(single))


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_release_decision_flags_change_evidence_and_survive_pagination_deduplication(flag):
    release = {"tag_name": "v0.1.0", "draft": False, "prerelease": False}
    changed = {**release, flag: True}
    before = stable_release_evidence([release])
    after = stable_release_evidence([changed])
    assert sha256_json(before) != sha256_json(after)
    assert before == stable_release_evidence([{**release, "assets": [{"download_count": 2}]}])
    path = "/repos/o/r/releases?per_page=100"
    first = "https://api.github.com" + path
    second = "https://api.github.com/repos/o/r/releases?page=2"
    pages = {
        first: ([release], {"link": f'<{second}>; rel="next"'}),
        second: ([changed, {**release, "assets": []}], {}),
    }
    values, _ = github_json(Pages(pages), path)
    assert len(values) == 2
    assert {value[flag] for value in values} == {False, True}
    reverse, _ = github_json(
        Pages({first: ([changed], pages[first][1]), second: ([release], {})}), path
    )
    assert stable_release_evidence(values) == stable_release_evidence(reverse)
    assert sha256_json(stable_release_evidence(values)) == sha256_json(
        stable_release_evidence(reverse)
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/repos/o/r/tags?page=2",
        "http://api.github.com/repos/o/r/tags?page=2",
        "https://api.github.com/repos/o/other/tags?page=2",
        "https://api.github.com/repos/o/r/releases?page=2",
        "https://user:secret@api.github.com/repos/o/r/tags?page=2",
    ],
)
def test_pagination_rejects_changed_origin_repository_or_path_before_fetch(target):
    first = "https://api.github.com/repos/o/r/tags?per_page=100"
    client = Pages({first: ([], {"Link": f'<{target}>; rel="next"'})})
    with pytest.raises(FetchError, match="pagination"):
        github_json(client, "/repos/o/r/tags?per_page=100")
    assert client.calls == [first]


def test_pagination_cap_and_cycles_fail_instead_of_truncating():
    path = "/repos/o/r/releases?per_page=100"
    first = "https://api.github.com" + path
    pages = {}
    for page in range(1, 22):
        url = first if page == 1 else f"https://api.github.com/repos/o/r/releases?page={page}"
        pages[url] = (
            [{"tag_name": f"v0.0.{page}"}],
            {"Link": f'<https://api.github.com/repos/o/r/releases?page={page + 1}>; rel="next"'},
        )
    client = Pages(pages)
    with pytest.raises(FetchError, match="pagination.*20"):
        github_json(client, path)
    assert len(client.calls) == 20
    last_url = "https://api.github.com/repos/o/r/releases?page=20"
    pages[last_url] = (pages[last_url][0], {})
    complete = Pages(pages)
    values, _ = github_json(complete, path)
    assert len(complete.calls) == len(values) == 20
    client = Pages({first: ([], {"link": f'<{first}>; rel="next"'})})
    with pytest.raises(FetchError, match="pagination"):
        github_json(client, path)
    assert client.calls == [first]


@pytest.mark.parametrize(
    "path,value",
    [
        ("/repos/o/r", []),
        ("/repos/o/r", {"default_branch": []}),
        ("/repos/o/r/commits/main", None),
        ("/repos/o/r/commits/main", {"sha": []}),
        ("/repos/o/r/commits?per_page=1", []),
        ("/repos/o/r/commits?per_page=1", [True]),
        ("/repos/o/r/tags?per_page=100", {}),
        ("/repos/o/r/tags?per_page=100", [3]),
        ("/repos/o/r/tags?per_page=100", [{"name": "v1", "commit": None}]),
        ("/repos/o/r/releases?per_page=100", [None]),
        ("/repos/o/r/releases?per_page=100", [{"tag_name": "v1", "published_at": []}]),
    ],
)
def test_github_malformed_shapes_fail_actionably(path, value):
    with pytest.raises(FetchError, match="GitHub.*schema") as caught:
        github_json(Pages({"https://api.github.com" + path: (value, {})}), path)
    assert caught.value.code == "invalid_schema"


@pytest.mark.parametrize("branch", ["tags", "releases", "commits"])
def test_commit_object_is_not_mistaken_for_a_paginated_list(branch):
    path = f"/repos/o/r/commits/{branch}"
    value, _ = github_json(Pages({"https://api.github.com" + path: ({"sha": "a" * 40}, {})}), path)
    assert value == {"sha": "a" * 40}


@pytest.mark.parametrize(
    "path,is_list",
    [
        ("/repos/o/r/commits/main", False),
        ("/repos/typesafe-ai/skills/commits?path=skills/typesafe-ai/SKILL.md&per_page=1", True),
    ],
)
@pytest.mark.parametrize(
    "sha", ["main", "../main", "a" * 39, "g" * 40, "a" * 41, "a" * 40 + "/file", None, 3]
)
def test_head_and_skill_commits_require_immutable_40_hex_identity(path, is_list, sha):
    value = [{"sha": sha}] if is_list else {"sha": sha}
    with pytest.raises(FetchError, match="GitHub.*schema.*40") as caught:
        github_json(Pages({"https://api.github.com" + path: (value, {})}), path)
    assert caught.value.code == "invalid_schema"


@pytest.mark.parametrize("sha", ["a" * 40, "ABCDEF0123" * 4])
@pytest.mark.parametrize("path", ["/repos/o/r/commits/main", "/repos/o/r/commits?per_page=1"])
def test_github_commit_identity_accepts_40_hex_sha(path, sha):
    value = [{"sha": sha}] if "/commits?" in path else {"sha": sha}
    received, _ = github_json(Pages({"https://api.github.com" + path: (value, {})}), path)
    assert received == value


def test_invalid_github_json_has_no_raw_body_traceback():
    path = "/repos/o/r"
    body = f"{SECRET_MARKER} invalid json".encode()
    with pytest.raises(FetchError, match="GitHub.*JSON") as caught:
        github_json(Pages({"https://api.github.com" + path: (body, {})}), path)
    assert caught.value.code == "invalid_json"
    assert SECRET_MARKER not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "kind,body",
    [
        ("python", b"private-token invalid JSON"),
        ("python", b"[]"),
        ("python", b'{"info": [], "releases": {}}'),
        ("python", b'{"info": {"version": null}, "releases": {}}'),
        ("python", b'{"info": {"version": " "}, "releases": {}}'),
        ("python", b'{"info": {"version": "0.1.0"}, "releases": []}'),
        ("python", b'{"info": {"version": "0.1.0"}, "releases": {"0.1.0": null}}'),
        ("python", b'{"info": {"version": "0.2.0"}, "releases": {"0.1.0": []}}'),
        ("python", b'{"info": {"version": "0.1.0"}, "releases": {}}'),
        ("javascript", b"private-token invalid JSON"),
        ("javascript", b"[]"),
        ("javascript", b'{"dist-tags": []}'),
        ("javascript", b'{"dist-tags": {"latest": null}}'),
        ("javascript", b'{"dist-tags": {"latest": ""}}'),
    ],
)
def test_registry_failures_are_structured_and_preserve_last_good_tree(tmp_path, kind, body):
    synchronize(tmp_path, PublicSources(), strict=True)
    baseline = snapshot(tmp_path)

    class BrokenRegistry(PublicSources):
        def get(self, url):
            if (kind == "python" and "pypi.org" in url) or (
                kind == "javascript" and "registry.npmjs.org" in url
            ):
                return Response(url, body, {})
            return super().get(url)

    with pytest.raises(RuntimeError, match="(PyPI|npm).*(JSON|schema)") as caught:
        synchronize(tmp_path, BrokenRegistry(), strict=True)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-token" not in rendered
    assert "AttributeError" not in rendered
    assert "last-known-good" in str(caught.value)
    assert snapshot(tmp_path) == baseline


def test_pypi_published_identity_must_exist_in_releases_map():
    response = Response(
        "https://pypi.org/pypi/typesafe-sdk/json",
        b'{"info": {"version": "0.2.0"}, "releases": {"0.1.0": []}}',
        {},
    )
    with pytest.raises(FetchError, match="PyPI.*schema.*info.version.*releases") as caught:
        registry_json(response, "python")
    assert caught.value.code == "invalid_schema"


@pytest.mark.parametrize(
    "body", [b"[]", b'{"engines": null}', b'{"engines": {"node": []}}', b"private-token"]
)
def test_malformed_javascript_config_fails_without_body_disclosure(body):
    class BrokenConfig(PublicSources):
        def get(self, url):
            if url.endswith("package.json"):
                return Response(url, body, {})
            return super().get(url)

    with pytest.raises(FetchError, match="SDK configuration") as caught:
        sdk_snapshot(
            BrokenConfig(),
            {},
            "typesafe-ai/typesafe-sdk-js",
            "@typesafe-ai/sdk",
            "javascript",
            "package.json",
            None,
            None,
        )
    assert "private-token" not in "".join(traceback.format_exception(caught.value))


def test_sdk_and_evaluation_tool_both_consume_all_pages():
    class PaginatedSources(PublicSources):
        def __init__(self):
            super().__init__()
            self.second_pages = []

        def get(self, url):
            if url.endswith("page=2"):
                self.second_pages.append(url)
                body = (
                    [{"name": "v0.0.1", "commit": {"sha": "c" * 40}}]
                    if "/tags?" in url
                    else [{"tag_name": "v0.0.1"}]
                )
                return Response(url, json.dumps(body).encode(), {})
            response = super().get(url)
            if "/tags?" in url or "/releases?" in url:
                return Response(
                    url, response.body, {"link": f'<{url.split("?")[0]}?page=2>; rel="next"'}
                )
            return response

    client = PaginatedSources()
    sdk = sdk_snapshot(
        client,
        {},
        "typesafe-ai/typesafe-sdk-python",
        "typesafe-sdk",
        "python",
        "pyproject.toml",
        None,
        None,
    )
    evaluation = evaluation_tool_snapshot(client, {}, None)
    assert sdk["available_tags"] == ["v0.0.1", "v0.1.0"]
    assert len(client.second_pages) == 4
    assert evaluation["source_tag"] == "v0.1.0"
