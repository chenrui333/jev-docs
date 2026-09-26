"""Synchronize public TypeSafe evidence into deterministic Jev state."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from jev_docs.inventories import cookbooks, model_limitations, render_limitations
from jev_docs.text import excerpt

ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = "https://docs.typesafe.ai"
DOCS_INDEX = f"{DOCS_ROOT}/llms.txt"
DOCS_SITEMAP = f"{DOCS_ROOT}/sitemap.xml"
GITHUB_API = "https://api.github.com"
USER_AGENT = "jev-docs-sync/0.1 (+https://github.com/chenrui333/jev-docs)"
SCHEMA = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode())


def utc_now() -> datetime:
    override = os.environ.get("JEVDOCS_OBSERVED_AT")
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def iso_day(value: datetime) -> str:
    return value.date().isoformat()


def normalize_url(url: str, base: str = DOCS_ROOT) -> str:
    resolved = urljoin(base.rstrip("/") + "/", url.strip())
    parts = urlsplit(resolved)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def docs_canonical_url(url: str) -> str:
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    path = parts.path[:-3] if parts.path.endswith(".md") else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path or "/", "", ""))


def docs_source_id(url: str) -> str:
    path = urlsplit(docs_canonical_url(url)).path.lstrip("/") or "index"
    return f"docs:{path}"


def source_url(source_id: str) -> str:
    if source_id.startswith("docs:"):
        return f"{DOCS_ROOT}/{source_id[5:]}"
    return source_id


@dataclass(frozen=True)
class Response:
    url: str
    body: bytes
    headers: dict[str, str]
    status: int = 200


class FetchError(RuntimeError):
    """A public source could not be fetched after bounded retries."""


class HTTPClient:
    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 2,
        timeout: float = 20.0,
    ) -> None:
        self.opener = opener or urlopen
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout

    def get(self, url: str) -> Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
                with self.opener(request, timeout=self.timeout) as raw:
                    body = raw.read()
                    headers = {key.lower(): value for key, value in raw.headers.items()}
                    return Response(str(raw.url), body, headers, getattr(raw, "status", 200))
            except HTTPError as exc:
                last_error = exc
                if exc.code < 500 and exc.code != 429:
                    break
            except (OSError, URLError) as exc:
                last_error = exc
            if attempt < self.retries:
                self.sleep(0.25 * (2**attempt))
        raise FetchError(f"failed to fetch {url}: {last_error}") from last_error


@dataclass
class Artifact:
    source_id: str
    source_type: str
    url: str
    content: bytes
    discovered_via: str | None = None
    upstream_last_modified: str | None = None
    upstream_commit: str | None = None
    source_tag: str | None = None

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.content)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def parse_llms(text: str, base: str = DOCS_ROOT) -> list[tuple[str, str, str]]:
    """Return unique same-host (url, title, description) entries in source order."""
    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"-\s*\[([^]]+)\]\(([^)]+)\)(?::\s*(.*))?", text):
        title, raw_url, description = match.groups()
        url = docs_canonical_url(normalize_url(raw_url, base))
        if urlsplit(url).netloc != "docs.typesafe.ai" or url in seen:
            continue
        seen.add(url)
        results.append((url, title.strip(), (description or "").strip()))
    return results


def parse_llms_exclusions(text: str, base: str = DOCS_ROOT) -> list[dict[str, str]]:
    """Record links intentionally excluded because they are outside the docs host."""
    exclusions: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"-\s*\[([^]]+)\]\(([^)]+)\)", text):
        title, raw_url = match.groups()
        url = normalize_url(raw_url, base)
        if urlsplit(url).netloc != "docs.typesafe.ai":
            exclusions[url] = {"url": url, "title": title.strip(), "reason": "external-host"}
    return [exclusions[url] for url in sorted(exclusions)]


def parse_sitemap(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    root = ET.fromstring(text)
    for url_node in root.iter():
        if not url_node.tag.endswith("url"):
            continue
        loc = next((child.text for child in list(url_node) if child.tag.endswith("loc")), None)
        if not loc:
            continue
        lastmod = next(
            (child.text for child in list(url_node) if child.tag.endswith("lastmod")), ""
        )
        result[docs_canonical_url(loc.strip())] = (lastmod or "").strip()
    return result


def title_from_markdown(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    return re.sub(r"[*`]", "", match.group(1)).strip() if match else fallback


PRACTICE_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "jev-complements-generative-agents",
        "category": "agent-integration",
        "summary": "Use coding agents to write software that calls Jev for bounded structured decisions such as routing, classification, scoring and guardrails. Jev does not replace their generative base model or write code or converse.",
        "patterns": [r"Jev is not a drop-in replacement"],
        "sources": ["docs:introduction/coding-agents"],
    },
    {
        "id": "prefer-typed-decisions-over-prompt-parsing",
        "category": "agent-integration",
        "summary": "For decisions expressible as TypeSafe's typed questions, consider replacing fragile generative return-JSON prompts with typed decision calls. This guidance does not cover arbitrary text generation or every JSON task.",
        "patterns": [r"Replace a fragile prompt.*return JSON"],
        "sources": ["docs:introduction/coding-agents"],
    },
    {
        "id": "deterministic-before-jev",
        "category": "decision-boundary",
        "summary": "Keep deterministic rules, calculations, exact lookups, control flow, and side effects in code; use Jev where semantic judgment is needed.",
        "patterns": [
            r"Keep control flow, deterministic rules, and side effects in code",
            r"Keep deterministic work in code",
        ],
        "sources": ["docs:concepts/how-to-build-with-system-one", "skill:typesafe-ai"],
    },
    {
        "id": "narrow-coherent-questions",
        "category": "question-design",
        "summary": "Ask narrow, coherent, typed questions with explicit instructions and criteria rather than hiding several judgments in one broad question.",
        "patterns": [
            r"Break broad judgments into narrow, typed questions",
            r"Ask one narrow, coherent judgment per question",
            r"Ask the most explicit, narrow, specific",
        ],
        "sources": ["docs:concepts/how-to-build-with-system-one", "skill:typesafe-ai"],
    },
    {
        "id": "batch-independent-questions",
        "category": "batching",
        "summary": "Ask independent questions over the same state together so the model can evaluate them in parallel and code can compose the answers.",
        "patterns": [
            r"Ask independent questions together",
            r"Ask independent questions over the same state together",
            r"evaluated in parallel",
        ],
        "sources": ["docs:concepts/state", "docs:patterns/fan-out", "skill:typesafe-ai"],
    },
    {
        "id": "explicit-speculative-premises",
        "category": "batching",
        "summary": "Speculative questions are acceptable in a batch, but their premises must be stated explicitly and code must decide which answers are relevant.",
        "patterns": [
            r"State each speculative premise explicitly",
            r"speculative questions.*premise",
            r"let your code decide",
        ],
        "sources": ["docs:patterns/fan-out", "skill:typesafe-ai"],
    },
    {
        "id": "choice-confidence-concentration",
        "category": "confidence",
        "summary": "Choice confidence summarizes concentration of the competing option probabilities; it is not a guarantee that the selected answer is correct.",
        "patterns": [r"confidence.*probability.*spread", r"Choice confidence", r"concentration"],
        "sources": ["docs:confidence", "skill:typesafe-ai"],
    },
    {
        "id": "confidence-is-not-permission",
        "category": "confidence",
        "summary": "Confidence is an uncertainty signal. Keep the permission to act, review, or escalate explicit in application code.",
        "patterns": [
            r"confidence.*not.*permission",
            r"Use probabilities and confidence to act",
            r"Your code encodes the risk tolerance",
        ],
        "sources": ["docs:confidence", "skill:typesafe-ai"],
    },
    {
        "id": "noul-is-yes-probability",
        "category": "primitives",
        "summary": "A Noul value is the probability that a yes/no proposition is true; a value near 0.5 means uncertainty between yes and no, not medium intensity.",
        "patterns": [
            r"not a scale of the thing",
            r"probability that the answer is yes",
            r"near 0\.5",
        ],
        "sources": ["docs:primitives/noul", "skill:typesafe-ai"],
    },
    {
        "id": "candidate-must-be-present",
        "category": "question-design",
        "summary": "Candidate selection questions can only select a value that is actually present in the candidate state or criteria.",
        "patterns": [
            r"model cannot choose an omitted value",
            r"candidate coverage",
            r"candidate.*actually",
        ],
        "sources": ["skill:typesafe-ai", "docs:primitives/choice"],
    },
    {
        "id": "domain-calibrated-thresholds",
        "category": "thresholds",
        "summary": "Choose thresholds from the application's data, model performance, and consequences; do not treat cookbook thresholds as universal defaults.",
        "patterns": [
            r"thresholds evaluated on the user's data",
            r"threshold values depend on your domain",
            r"consequences",
        ],
        "sources": ["docs:confidence", "skill:typesafe-ai"],
    },
    {
        "id": "typed-output-is-not-truth",
        "category": "verification",
        "summary": "Typed output guarantees interface shape, not truth; test representative cases and resulting application behavior.",
        "patterns": [
            r"Typed output guarantees the interface, not truth",
            r"not a guarantee that the answer is correct",
            r"Test representative cases",
        ],
        "sources": ["docs:concepts/system-one", "skill:typesafe-ai"],
    },
    {
        "id": "observed-state-distinct-from-inference",
        "category": "state",
        "summary": "Keep observed application state and inferred model judgments conceptually distinct, and check freshness before applying a result to changed state.",
        "patterns": [
            r"Keep inferred state distinct from observed facts",
            r"state contains the content",
            r"freshness before applying",
        ],
        "sources": ["docs:concepts/state", "skill:typesafe-ai"],
    },
    {
        "id": "diagnose-failure-layers",
        "category": "verification",
        "summary": "For failures, inspect the exact state, questions, candidates, answers, composition, and observed outcome; separate evidence, model, code, and service failures.",
        "patterns": [
            r"Separate missing evidence, model errors, code errors, and service failures",
            r"inspect the exact state",
            r"failures",
        ],
        "sources": ["skill:typesafe-ai", "docs:concepts/how-to-build-with-system-one"],
    },
)


def rule_evidence(
    rule: dict[str, Any], contents: dict[str, str], artifact_by_id: dict[str, Artifact]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for source_id in rule["sources"]:
        content = contents.get(source_id)
        if content is None:
            continue
        for pattern in rule["patterns"]:
            found = excerpt(content, pattern)
            if found:
                artifact = artifact_by_id.get(source_id)
                evidence.append(
                    {
                        "source_id": source_id,
                        "url": artifact.url if artifact else source_url(source_id),
                        "sha256": artifact.sha256 if artifact else None,
                        "excerpt": found,
                    }
                )
                break
    return evidence


def old_item_map(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(item[key]): item
        for item in value.get("items", [])
        if isinstance(item, dict) and key in item
    }


def observed_state(value: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    payload = copy.deepcopy(value)
    payload.pop("observed_at", None)
    fingerprint = sha256_json(payload)
    if previous and previous.get("fingerprint") == fingerprint:
        payload["observed_at"] = previous.get("observed_at")
    else:
        payload["observed_at"] = utc_now().isoformat().replace("+00:00", "Z")
    payload["fingerprint"] = fingerprint
    return payload


def actionable_discrepancies(freshness: dict[str, Any], now: datetime) -> list[str]:
    observed_at = freshness.get("observed_at")
    grace_days = freshness.get("grace_period_days", 3)
    if not isinstance(observed_at, str) or not isinstance(grace_days, int):
        return []
    try:
        first_seen = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return []
    if now - first_seen < timedelta(days=grace_days):
        return []
    return [
        name
        for name in ("python", "javascript")
        if freshness.get(name, {}).get("status") == "discrepancy"
    ]


def artifact_manifest(
    artifact: Artifact, previous: dict[str, Any] | None, title: str = "", description: str = ""
) -> dict[str, Any]:
    same = previous and previous.get("sha256") == artifact.sha256
    return {
        "source_id": artifact.source_id,
        "source_type": artifact.source_type,
        "canonical_url": docs_canonical_url(artifact.url)
        if artifact.url.startswith(DOCS_ROOT)
        else artifact.url,
        "fetch_url": artifact.url,
        "discovered_via": artifact.discovered_via,
        "upstream_last_modified": artifact.upstream_last_modified,
        "upstream_commit": artifact.upstream_commit,
        "source_tag": artifact.source_tag,
        "sha256": artifact.sha256,
        "bytes": len(artifact.content),
        "title": title,
        "description": description,
        "observed_at": previous.get("observed_at")
        if same
        else utc_now().isoformat().replace("+00:00", "Z"),
    }


def github_json(client: HTTPClient, path: str) -> tuple[Any, Response]:
    response = client.get(f"{GITHUB_API}{path}")
    return json.loads(response.body), response


def evidence_bytes(value: Any) -> bytes:
    return canonical_json(value).encode()


def stable_repository_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"malformed": type(value).__name__}
    return {
        key: value.get(key)
        for key in ("full_name", "default_branch", "archived", "visibility", "license")
    }


def stable_tag_evidence(value: Any) -> Any:
    if not isinstance(value, list):
        return {"malformed": type(value).__name__}
    return sorted(
        (
            {"name": item.get("name"), "commit": {"sha": item.get("commit", {}).get("sha")}}
            if isinstance(item, dict) and isinstance(item.get("commit"), dict)
            else {"malformed": True}
            for item in value
        ),
        key=canonical_json,
    )


def stable_release_evidence(value: Any) -> Any:
    if not isinstance(value, list):
        return {"malformed": type(value).__name__}
    return sorted(
        (
            {key: item.get(key) for key in ("tag_name", "published_at", "name", "html_url")}
            if isinstance(item, dict)
            else {"malformed": True}
            for item in value
        ),
        key=canonical_json,
    )


def stable_head_evidence(value: Any) -> dict[str, Any]:
    return {"sha": value.get("sha")} if isinstance(value, dict) else {"malformed": True}


def parse_simple_toml_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)
    return match.group(1) if match else None


def parse_release_notes(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    text = re.sub(r"<h2\b[^>]*>\s*(.*?)\s*</h2>", r"## \1", text, flags=re.S)
    headings = list(
        re.finditer(r"^\s*#+\s*v?(\d+\.\d+\.\d+)\s*\(([^)]+)\)", text, re.MULTILINE | re.IGNORECASE)
    )
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[match.end() : end]
        bullets = [
            re.sub(r"\s+", " ", line.strip()[2:].strip())
            for line in block.splitlines()
            if line.strip().startswith(("-", "*"))
        ]
        result.append({"version": match.group(1), "date": match.group(2), "notes": bullets[:12]})
    return result


def version_tuple(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def release_provenance(version: Any, tags: Any, releases: Any) -> dict[str, Any]:
    """Reconcile registry, tag, and release evidence without choosing silently."""
    discrepancies: list[str] = []
    malformed: list[str] = []
    tag_map: dict[str, str] = {}
    if not isinstance(tags, list):
        malformed.append("tags_not_a_list")
        tags = []
    for index, entry in enumerate(tags):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            malformed.append(f"tag_entry_{index}_malformed")
            continue
        commit = entry.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str):
            malformed.append(f"tag_entry_{index}_missing_commit")
            continue
        tag_map[entry["name"]] = sha
    release_records: list[dict[str, Any]] = []
    if not isinstance(releases, list):
        malformed.append("releases_not_a_list")
        releases = []
    for index, entry in enumerate(releases):
        if not isinstance(entry, dict) or not isinstance(entry.get("tag_name"), str):
            malformed.append(f"release_entry_{index}_malformed")
            continue
        release_records.append(entry)
    expected_tag = f"v{version}" if version_tuple(version) else None
    matching_release = next(
        (entry for entry in release_records if entry["tag_name"] == expected_tag), None
    )
    if expected_tag and expected_tag not in tag_map:
        discrepancies.append("registry_version_missing_git_tag")
    if expected_tag and matching_release is None:
        discrepancies.append("registry_version_missing_github_release")
    registry_version = version_tuple(version)
    source_versions = [
        version_tuple(entry["tag_name"])
        for entry in release_records
        if version_tuple(entry["tag_name"])
    ] + [version_tuple(name) for name in tag_map if version_tuple(name)]
    if registry_version and source_versions and max(source_versions) > registry_version:
        discrepancies.append("git_source_ahead_of_registry")
    if registry_version and source_versions and registry_version > max(source_versions):
        discrepancies.append("registry_ahead_of_git_source")
    if malformed:
        discrepancies.append("malformed_release_metadata")
    return {
        "source_tag": expected_tag if expected_tag in tag_map else None,
        "source_commit": tag_map.get(expected_tag) if expected_tag else None,
        "release_history": [
            {
                "tag": entry["tag_name"],
                "version": entry["tag_name"].removeprefix("v"),
                "source_commit": tag_map.get(entry["tag_name"]),
                "published_at": entry.get("published_at"),
                "name": entry.get("name"),
                "source_url": entry.get("html_url"),
            }
            for entry in release_records
        ],
        "available_tags": sorted(tag_map),
        "discrepancies": sorted(set(discrepancies)),
        "malformed_metadata": malformed,
    }


def extract_models(content: str) -> list[dict[str, str]]:
    """Extract only the explicit model/alias tables from the models page."""
    models: list[dict[str, str]] = []
    for match in re.finditer(r"^\|\s*([^|`]+?)\s*\|\s*`([^`]+)`\s*\|", content, re.MULTILINE):
        label, model_id = match.groups()
        if label.strip().lower() in {"jev 1.13", "jev 1.13.0"} or label.strip().lower().startswith(
            "jev "
        ):
            models.append({"kind": "versioned", "id": model_id, "label": label.strip()})
    for match in re.finditer(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", content, re.MULTILINE):
        alias, target = match.groups()
        models.append({"kind": "alias", "id": alias, "target": target})
    return sorted(models, key=lambda item: (item["kind"], item["id"]))


# Explicit release-note rules, deliberately not an API compatibility analyzer.
PYTHON_CAPABILITY_RULES = (
    (
        "python-pydantic-serialization",
        r"ser/de library.*pydantic",
        "Pydantic serialization replaces msgspec.",
    ),
    (
        "python-response-model",
        r"system_one.*response_model",
        "system_one accepts a Pydantic response_model for additional type safety.",
    ),
    (
        "python-api-key-validation",
        r"validate the API key early and exclude the value from logged exceptions",
        "Validate API keys early without including their values in logged exceptions.",
    ),
    (
        "python-gateway-examples",
        r"examples for usage with AI gateways",
        "Documented examples for using the Python SDK with AI gateways.",
    ),
    ("python-http2-extra", r"add.*http2.*extra", "Optional typesafe-sdk[http2] dependencies."),
    ("python-http2-usage", r"document.*HTTP/2", "Documented HTTP/2 usage for the Python SDK."),
)


def python_capabilities(client, artifacts, repo, releases, registry, live_notes, previous=()):
    """Only attribute a capability to a registry version with matching immutable source."""
    items = []
    for release in releases:
        version = release["version"]
        live = next((n for n in live_notes if n["version"] == version), None)
        if (
            not live
            or not release.get("source_commit")
            or version not in registry.get("releases", {})
        ):
            continue
        rules = [
            rule
            for rule in PYTHON_CAPABILITY_RULES
            if any(re.search(rule[1], note, re.I) for note in live["notes"])
        ]
        if not rules:
            continue
        commit = release["source_commit"]
        evidence = []
        for path in ("pyproject.toml", "docs/changelog.md"):
            url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
            response = client.get(url)
            sid = f"github:{repo}:{release['tag']}:{path}"
            artifact = Artifact(
                sid,
                "github-source",
                url,
                response.body,
                upstream_commit=commit,
                source_tag=release["tag"],
            )
            artifacts[sid] = artifact
            evidence.append({"source_id": sid, "url": url, "sha256": artifact.sha256})
            if path == "pyproject.toml":
                if parse_simple_toml_value(response.body.decode(), "version") != version:
                    raise RuntimeError(
                        f"release config does not match registry version: {repo} {version}"
                    )
            else:
                notes = next(
                    (
                        n
                        for n in parse_release_notes(response.body.decode())
                        if n["version"] == version
                    ),
                    None,
                )
        if not notes:
            raise RuntimeError(f"immutable release changelog lacks {version}")
        for ident, pattern, summary in rules:
            matching = [note for note in notes["notes"] if re.search(pattern, note, re.I)]
            if matching:
                items.append(
                    {
                        "id": ident,
                        "introduced": version,
                        "status": "documented",
                        "summary": summary,
                        "source_tag": release["tag"],
                        "source_commit": commit,
                        "evidence": matching,
                        "provenance": evidence,
                    }
                )
    # Later notes can mention the same feature again; preserve its earliest
    # verified introduction rather than creating duplicate identities.
    by_id = {}
    for item in sorted(items, key=lambda item: version_tuple(item["introduced"])):
        by_id.setdefault(item["id"], item)
    for item in previous:
        if item["id"] not in by_id:
            by_id[item["id"]] = {**item, "status": "unknown"}
    return [by_id[key] for key in sorted(by_id)]


def sdk_snapshot(
    client: HTTPClient,
    artifacts: dict[str, Artifact],
    repo: str,
    package: str,
    package_kind: str,
    config_path: str,
    changelog_content: str | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata_path = f"/repos/{repo}"
    repo_meta, repo_response = github_json(client, metadata_path)
    artifacts[f"github:{repo}:repository"] = Artifact(
        f"github:{repo}:repository",
        "github-api",
        f"{GITHUB_API}{metadata_path}",
        evidence_bytes(stable_repository_evidence(repo_meta)),
    )
    tags_path = f"/repos/{repo}/tags?per_page=100"
    tags, tags_response = github_json(client, tags_path)
    artifacts[f"github:{repo}:tags"] = Artifact(
        f"github:{repo}:tags",
        "github-api",
        f"{GITHUB_API}{tags_path}",
        evidence_bytes(stable_tag_evidence(tags)),
    )
    releases_path = f"/repos/{repo}/releases?per_page=100"
    releases, releases_response = github_json(client, releases_path)
    artifacts[f"github:{repo}:releases"] = Artifact(
        f"github:{repo}:releases",
        "github-api",
        f"{GITHUB_API}{releases_path}",
        evidence_bytes(stable_release_evidence(releases)),
    )
    branch = repo_meta.get("default_branch", "main") if isinstance(repo_meta, dict) else "main"
    head_path = f"/repos/{repo}/commits/{branch}"
    head, head_response = github_json(client, head_path)
    artifacts[f"github:{repo}:head"] = Artifact(
        f"github:{repo}:head",
        "github-api",
        f"{GITHUB_API}{head_path}",
        evidence_bytes(stable_head_evidence(head)),
    )
    raw_url = f"https://raw.githubusercontent.com/{repo}/{head['sha']}/{config_path}"
    config_response = client.get(raw_url)
    config_artifact = Artifact(
        f"github:{repo}:{config_path}",
        "github-source",
        raw_url,
        config_response.body,
        upstream_commit=head["sha"],
    )
    artifacts[config_artifact.source_id] = config_artifact
    if package_kind == "python":
        registry_url = f"https://pypi.org/pypi/{package}/json"
        registry_response = client.get(registry_url)
        registry = json.loads(registry_response.body)
        runtime = (
            parse_simple_toml_value(config_response.body.decode(), "requires-python") or "unknown"
        )
        public_surface = ["TypeSafeClient", "AsyncTypeSafeClient", "Choice", "Noul", "Score"]
        registry_id = f"registry:pypi:{package}"
    else:
        registry_url = f"https://registry.npmjs.org/{package.replace('/', '%2f')}"
        registry_response = client.get(registry_url)
        registry = json.loads(registry_response.body)
        config = json.loads(config_response.body)
        runtime = config.get("engines", {}).get("node", "unknown")
        public_surface = ["TypeSafeClient", "choice", "noul", "score"]
        registry_id = f"registry:npm:{package}"
    if package_kind == "python":
        version = registry.get("info", {}).get("version") if isinstance(registry, dict) else None
        registry_evidence = {
            "version": version,
            "published_versions": sorted(registry.get("releases", {})),
        }
    else:
        dist_tags = registry.get("dist-tags", {}) if isinstance(registry, dict) else {}
        version = dist_tags.get("latest") if isinstance(dist_tags, dict) else None
        registry_evidence = {"latest": version}
    artifacts[registry_id] = Artifact(
        registry_id, "package-registry", registry_url, evidence_bytes(registry_evidence)
    )
    release_info = release_provenance(version, tags, releases)
    release_history = release_info["release_history"]
    notes = parse_release_notes(changelog_content or "")
    for item in release_history:
        note = next((note for note in notes if note["version"] == item["version"]), None)
        if note and item.get("source_commit"):
            item["documented_changes"] = note["notes"]
            sid = f"docs:sdk/{package_kind}/changelog"
            if sid in artifacts:
                item["documented_changes_provenance"] = [
                    {"source_id": sid, "url": artifacts[sid].url, "sha256": artifacts[sid].sha256}
                ]
    state = {
        "schema_version": SCHEMA,
        "package": package,
        "repository": f"https://github.com/{repo}",
        "version": version,
        "source_tag": release_info["source_tag"],
        "source_commit": release_info["source_commit"],
        "main_commit": head.get("sha"),
        "runtime": {package_kind: runtime},
        "public_surface": {
            "entry_points": public_surface,
            "primitives": ["Choice", "Noul", "Score"],
        },
        "release_history": sorted(
            release_history,
            key=lambda item: (item.get("published_at") or "", item.get("tag") or ""),
        ),
        "available_tags": release_info["available_tags"],
        "provenance": [
            {
                "source_id": config_artifact.source_id,
                "url": config_artifact.url,
                "sha256": config_artifact.sha256,
            },
            {
                "source_id": registry_id,
                "url": registry_url,
                "sha256": artifacts[registry_id].sha256,
            },
        ],
        "capabilities": python_capabilities(
            client,
            artifacts,
            repo,
            release_history,
            registry,
            notes,
            (previous or {}).get("capabilities", []),
        )
        if package_kind == "python"
        else [],
        "discrepancies": release_info["discrepancies"],
        "malformed_release_metadata": release_info["malformed_metadata"],
    }
    return observed_state(state, previous)


def evaluation_tool_snapshot(client, artifacts, previous):
    repo = "typesafe-ai/system-one-adapter-python"
    metadata = {}
    for surface, suffix, projection in (
        ("repository", "", stable_repository_evidence),
        ("tags", "/tags?per_page=100", stable_tag_evidence),
        ("releases", "/releases?per_page=100", stable_release_evidence),
    ):
        value, response = github_json(client, f"/repos/{repo}{suffix}")
        metadata[surface] = value
        sid = f"github:{repo}:{surface}"
        artifacts[sid] = Artifact(
            sid, "official_evaluation_tool", response.url, evidence_bytes(projection(value))
        )
    releases = [
        item
        for item in metadata["releases"]
        if version_tuple(item.get("tag_name"))
        and not item.get("prerelease")
        and not item.get("draft")
    ]
    if not releases:
        raise RuntimeError("evaluation tool has no stable release evidence")
    release = max(releases, key=lambda item: version_tuple(item["tag_name"]))
    version = release["tag_name"].removeprefix("v")
    info = release_provenance(version, metadata["tags"], releases)
    if not info["source_commit"]:
        raise RuntimeError("evaluation tool release has no matching immutable tag")
    sources = []
    for path in ("README.md", "pyproject.toml"):
        url = f"https://raw.githubusercontent.com/{repo}/{info['source_commit']}/{path}"
        response = client.get(url)
        sid = f"github:{repo}:{path}"
        artifact = Artifact(
            sid,
            "official_evaluation_tool",
            url,
            response.body,
            upstream_commit=info["source_commit"],
            source_tag=info["source_tag"],
        )
        artifacts[sid] = artifact
        sources.append({"source_id": sid, "url": url, "sha256": artifact.sha256})
        if path == "README.md":
            readme = response.body.decode()
        elif parse_simple_toml_value(response.body.decode(), "version") != version:
            raise RuntimeError("evaluation tool release/config mismatch")
    purpose = excerpt(readme, r"Useful for comparing TypeSafe")
    compatibility = excerpt(readme, r"drop-in replacement", 420)
    if not purpose or not compatibility:
        raise RuntimeError("evaluation tool purpose/compatibility evidence unrecognized")
    providers = [
        name
        for extra, name in (
            ("openai", "OpenAI-compatible"),
            ("anthropic", "Anthropic"),
            ("gemini", "Gemini"),
        )
        if f"system-one-adapter[{extra}]" in readme
    ]
    return observed_state(
        {
            "schema_version": SCHEMA,
            "source_class": "official_evaluation_tool",
            "repository": f"https://github.com/{repo}",
            "version": version,
            "source_tag": info["source_tag"],
            "source_commit": info["source_commit"],
            "purpose": purpose,
            "compatibility": compatibility,
            "provider_families": providers,
            "provenance": sources,
            "scope": "Official LLM-backed comparison tool; not Jev model state, API contract, SDK release stream, or benchmark evidence.",
        },
        previous,
    )


def derive_practices(
    contents: dict[str, str], artifacts: dict[str, Artifact], previous: dict[str, Any] | None
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    old = old_item_map(previous, "id")
    for rule in PRACTICE_RULES:
        evidence = rule_evidence(rule, contents, artifacts)
        item = {
            "id": rule["id"],
            "category": rule["category"],
            "status": "recommended" if evidence else "unknown",
            "summary": rule["summary"],
            "first_seen": old.get(rule["id"], {}).get("first_seen"),
            "last_verified": old.get(rule["id"], {}).get("last_verified") or iso_day(utc_now()),
            "sources": evidence,
            "interpretation": "Deterministic summary of matching canonical guidance; not an upstream quote.",
        }
        items.append(item)
    state = {
        "schema_version": SCHEMA,
        "status_vocabulary": [
            "recommended",
            "discouraged",
            "anti_pattern",
            "deprecated",
            "unknown",
        ],
        "items": items,
    }
    result = observed_state(state, previous)
    if previous and result["fingerprint"] == previous.get("fingerprint"):
        result["items"] = previous["items"]
    return result


def derive_state(
    contents: dict[str, str], artifacts: dict[str, Artifact], previous_state: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    def provenance(source_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [
            {
                "source_id": source_id,
                "url": artifacts[source_id].url,
                "sha256": artifacts[source_id].sha256,
            }
            for source_id in source_ids
            if source_id in artifacts
        ]

    primitive_defs = [
        ("choice", "Select one option from a defined set.", "Choice", "primitives/choice"),
        (
            "noul",
            "Evaluate whether a yes/no proposition is true and return its yes probability.",
            "Noul",
            "primitives/noul",
        ),
        ("score", "Rate content against ordered, descriptive levels.", "Score", "primitives/score"),
    ]
    primitive_items = []
    for ident, summary, title, page in primitive_defs:
        content = contents.get(f"docs:{page}", "")
        primitive_items.append(
            {
                "id": ident,
                "name": title,
                "summary": summary,
                "observed_title": title_from_markdown(content, title),
                "source": {
                    "source_id": f"docs:{page}",
                    "url": f"{DOCS_ROOT}/{page}",
                    "sha256": artifacts[f"docs:{page}"].sha256
                    if f"docs:{page}" in artifacts
                    else None,
                },
            }
        )
    primitives = observed_state(
        {"schema_version": SCHEMA, "items": primitive_items}, previous_state.get("primitives")
    )
    api = observed_state(
        {
            "schema_version": SCHEMA,
            "endpoint": "POST https://api.typesafe.ai/v1/systemone",
            "request_fields": ["model", "state", "questions"],
            "question_types": ["choice", "score", "noul"],
            "model_default": "jev-latest",
            "response_shape": "answers map keyed by question id",
            "provenance": provenance(["docs:api", "docs:migrating-to-v1"]),
        },
        previous_state.get("api"),
    )
    models = observed_state(
        {
            "schema_version": SCHEMA,
            "models": extract_models(contents.get("docs:models", "")),
            "provenance": provenance(["docs:models"]),
        },
        previous_state.get("models"),
    )
    skill_artifact = artifacts.get("skill:typesafe-ai")
    skill = observed_state(
        {
            "schema_version": SCHEMA,
            "commit": skill_artifact.upstream_commit if skill_artifact else None,
            "source": {
                "source_id": skill_artifact.source_id if skill_artifact else "skill:typesafe-ai",
                "url": skill_artifact.url if skill_artifact else source_url("skill:typesafe-ai"),
                "sha256": skill_artifact.sha256 if skill_artifact else None,
            },
        },
        previous_state.get("skill"),
    )
    return {
        "primitives": primitives,
        "api": api,
        "models": models,
        "practices": derive_practices(contents, artifacts, previous_state.get("practices")),
        "skill": skill,
    }


def event_id(entity: str, change: str, before: str | None, after: str | None) -> str:
    return hashlib.sha256(
        f"jev-docs-event-v1|{entity}|{change}|{before or ''}|{after or ''}".encode()
    ).hexdigest()[:24]


def semantic_events(
    old_state: dict[str, Any], new_state: dict[str, Any], observed_at: str
) -> list[dict[str, Any]]:
    specs = {
        "primitives": ("primitive", "id"),
        "practices": ("practice", "id"),
        "api": ("api", "singleton"),
        "models": ("model", "singleton"),
        "sdk-python": ("sdk", "singleton"),
        "sdk-javascript": ("sdk", "singleton"),
        "skill": ("skill", "singleton"),
        "model-limitations": ("model_limitation", "id"),
        "cookbooks": ("cookbook", "id"),
        "evaluation-tool": ("evaluation_tool", "singleton"),
    }
    events: list[dict[str, Any]] = []
    for name, (category, key_mode) in specs.items():
        if (
            name in {"model-limitations", "cookbooks", "evaluation-tool"}
            and old_state.get(name) is None
            and new_state.get(name)
        ):
            value = new_state[name]
            baseline_items = value.get("items", [value])
            after = sha256_json([semantic_projection(name, item) for item in baseline_items])
            events.append(
                {
                    "schema_version": SCHEMA,
                    "id": event_id(name, "baseline", None, after),
                    "observed_at": observed_at,
                    "category": category,
                    "entity": name,
                    "change": "baseline",
                    "summary": f"Established {name} tracking with {len(baseline_items)} tracked entries; no historical additions inferred.",
                    "before_sha256": None,
                    "after_sha256": after,
                    "sources": [
                        source for item in baseline_items for source in item.get("provenance", [])
                    ],
                }
            )
            continue
        old_items = old_item_map(old_state.get(name), "id")
        new_items = old_item_map(new_state.get(name), "id")
        if key_mode == "singleton":
            old_value = old_state.get(name)
            new_value = new_state.get(name)
            old_items = {name: old_value} if old_value else {}
            new_items = {name: new_value} if new_value else {}
        for entity_key in sorted(set(old_items) | set(new_items)):
            before_value = semantic_projection(name, old_items.get(entity_key))
            after_value = semantic_projection(name, new_items.get(entity_key))
            before = sha256_json(before_value) if entity_key in old_items else None
            after = sha256_json(after_value) if entity_key in new_items else None
            if before == after:
                continue
            change = "added" if before is None else "removed" if after is None else "modified"
            item = new_items.get(entity_key, old_items.get(entity_key, {}))
            events.append(
                {
                    "schema_version": SCHEMA,
                    "id": event_id(f"{name}:{entity_key}", change, before, after),
                    "observed_at": observed_at,
                    "category": category,
                    "entity": entity_key,
                    "change": change,
                    "summary": item.get("summary")
                    or (
                        f"{item.get('package')} {old_items.get(entity_key, {}).get('version', 'unknown')} → {item.get('version')}; release guidance and capability evidence updated."
                        if name.startswith("sdk-")
                        else f"{name} {entity_key} changed"
                    ),
                    "before_sha256": before,
                    "after_sha256": after,
                    "sources": item.get("sources")
                    or item.get("provenance")
                    or ([item["source"]] if item.get("source") else []),
                }
            )
    return events


def practice_projection(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {key: item.get(key) for key in ("id", "category", "status", "summary", "first_seen")}


def semantic_projection(name: str, item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    if name in {"model-limitations", "cookbooks"}:
        return {
            key: value for key, value in item.items() if key not in {"provenance", "last_reviewed"}
        }
    if name == "practices":
        return practice_projection(item)
    if name == "primitives":
        return {key: item.get(key) for key in ("id", "name", "summary", "observed_title")}
    if name == "models":
        return {"models": item.get("models", [])}
    if name == "api":
        return {
            key: item.get(key)
            for key in (
                "endpoint",
                "request_fields",
                "question_types",
                "model_default",
                "response_shape",
            )
        }
    if name.startswith("sdk-"):
        projection = {
            key: item.get(key)
            for key in (
                "package",
                "version",
                "source_tag",
                "source_commit",
                "runtime",
                "public_surface",
                "release_history",
                "capabilities",
                "discrepancies",
            )
        }
        # Provenance enrichment alone is not SDK behavior/release evolution.
        projection["release_history"] = [
            {
                key: value
                for key, value in release.items()
                if key not in {"source_commit", "documented_changes_provenance"}
            }
            for release in item.get("release_history", [])
        ]
        projection["capabilities"] = [
            {key: value for key, value in capability.items() if key != "provenance"}
            for capability in item.get("capabilities", [])
        ]
        return projection
    if name == "evaluation-tool":
        return {
            key: value
            for key, value in item.items()
            if key not in {"provenance", "observed_at", "fingerprint"}
        }
    if name == "skill":
        return {"commit": item.get("commit")}
    return item


def source_events(
    old_manifest: dict[str, Any], new_manifest: dict[str, Any], observed_at: str
) -> list[dict[str, Any]]:
    def docs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            item["source_id"]: item
            for item in manifest.get("artifacts", [])
            if item.get("source_type") == "documentation" and item.get("source_id")
        }

    old_docs = docs(old_manifest)
    new_docs = docs(new_manifest)
    events: list[dict[str, Any]] = []
    for source_id in sorted(set(old_docs) | set(new_docs)):
        before_item = old_docs.get(source_id)
        after_item = new_docs.get(source_id)
        before = before_item.get("sha256") if before_item else None
        after = after_item.get("sha256") if after_item else None
        if before == after:
            continue
        change = "added" if before is None else "removed" if after is None else "modified"
        item = after_item or before_item
        events.append(
            {
                "schema_version": SCHEMA,
                "id": event_id(f"source:{source_id}", change, before, after),
                "observed_at": observed_at,
                "category": "documentation",
                "entity": source_id,
                "change": change,
                "summary": f"Documentation page {source_id} {change} in successful discovery.",
                "before_sha256": before,
                "after_sha256": after,
                "sources": [
                    {
                        "source_id": source_id,
                        "url": item.get("canonical_url"),
                        "sha256": item.get("sha256"),
                    }
                ],
            }
        )
    return events


def deduplicate_events(
    candidates: Iterable[dict[str, Any]], existing_ids: set[str]
) -> list[dict[str, Any]]:
    seen = set(existing_ids)
    result = []
    for event in sorted(candidates, key=lambda item: item["id"]):
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        result.append(event)
    return result


def rollup_events(
    existing: Iterable[dict[str, Any]], new_events: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {event["id"]: event for event in existing}
    by_id.update({event["id"]: event for event in new_events})
    return [by_id[event_id] for event_id in sorted(by_id)]


def load_history_events(events_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in sorted(events_dir.glob("*.json")):
        data = read_json(path, {})
        ids.update(
            event["id"]
            for event in data.get("events", [])
            if isinstance(event, dict) and event.get("id")
        )
    return ids


def render_best_practices(practices: dict[str, Any]) -> str:
    latest = max(
        (item.get("last_verified") or "unknown" for item in practices["items"]), default="unknown"
    )
    lines = [
        "# Jev Engineering Best Practices",
        "",
        f"Last verified: {latest}",
        "",
        "Generated from [`state/practices.json`](state/practices.json). `recommended` means current canonical evidence matched a transparent extraction rule; `unknown` means this snapshot does not provide enough evidence.",
        "",
    ]
    section_names = {
        "decision-boundary": "Programming model",
        "question-design": "Question construction",
        "batching": "Batching and speculative fan-out",
        "confidence": "Confidence",
        "primitives": "Primitives",
        "thresholds": "Thresholds and evaluation",
        "verification": "Error handling and verification",
        "state": "State construction",
    }
    for category in sorted({item["category"] for item in practices["items"]}):
        lines.extend([f"## {section_names.get(category, category.replace('-', ' ').title())}", ""])
        for item in sorted(
            (x for x in practices["items"] if x["category"] == category), key=lambda x: x["id"]
        ):
            lines.extend([f"### {item['id']} — `{item['status']}`", "", item["summary"], ""])
            if item["sources"]:
                lines.append("Evidence:")
                for evidence in item["sources"]:
                    lines.append(
                        f"- [{evidence['source_id']}]({evidence['url']}) — {evidence['excerpt']}"
                    )
            else:
                lines.append("Evidence: no matching canonical source in this observation.")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_daily_report(day: str, events: list[dict[str, Any]]) -> str:
    lines = [
        f"# Jev changes — {day}",
        "",
        "Semantic changes observed in official TypeSafe sources:",
        "",
    ]
    for event in sorted(events, key=lambda item: item["id"]):
        lines.append(
            f"- **{event['category']} / {event['entity']}** (`{event['change']}`): {event['summary']}"
        )
    lines.extend(
        [
            "",
            "Each event retains before/after hashes and source provenance in the matching JSON file.",
            "",
        ]
    )
    return "\n".join(lines)


def build_coverage(
    discovered: list[tuple[str, str, str]],
    artifacts: dict[str, Artifact],
    index: Artifact,
    discovery_method: str,
    sitemap_warning: str | None,
    previous: dict[str, Any] | None = None,
    intentionally_excluded: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    pages = []
    for url, title, _description in discovered:
        sid = docs_source_id(url)
        artifact = artifacts.get(sid)
        pages.append(
            {
                "source_id": sid,
                "canonical_url": docs_canonical_url(url),
                "title": title,
                "status": "fetched" if artifact else "failed",
                "sha256": artifact.sha256 if artifact else None,
            }
        )
    current_ids = {page["source_id"] for page in pages}
    previous_ids = {
        page["source_id"]
        for page in (previous or {}).get("pages", [])
        if isinstance(page, dict) and page.get("source_id")
    }
    return {
        "schema_version": SCHEMA,
        "complete": all(page["status"] == "fetched" for page in pages),
        "removal_safe": discovery_method == "llms.txt",
        "discovery": {
            "url": index.url,
            "source_id": index.source_id,
            "sha256": index.sha256,
            "method": discovery_method,
            "sitemap_warning": sitemap_warning,
        },
        "discovered_count": len(discovered),
        "fetched_count": sum(page["status"] == "fetched" for page in pages),
        "failed_count": sum(page["status"] == "failed" for page in pages),
        "retained_count": sum(page["status"] == "fetched" for page in pages),
        "newly_discovered": sorted(current_ids - previous_ids),
        "disappeared": sorted(previous_ids - current_ids),
        "intentionally_excluded": intentionally_excluded or [],
        "pages": pages,
    }


def validate_coverage(coverage: dict[str, Any]) -> None:
    """Validate completeness, not whether a successful observation found changes."""
    pages = coverage["pages"]
    ids = {page["source_id"] for page in pages}
    urls = {page["canonical_url"] for page in pages}
    if len(ids) != len(pages) or len(urls) != len(pages):
        raise RuntimeError("duplicate documentation page in coverage")
    if not coverage.get("complete") or not coverage.get("removal_safe"):
        raise RuntimeError("source coverage is incomplete or not removal-safe")
    if any(page["status"] != "fetched" or not page["sha256"] for page in pages):
        raise RuntimeError("source coverage contains unfetched pages")
    if (
        any(
            coverage[key] != len(pages)
            for key in ("discovered_count", "fetched_count", "retained_count")
        )
        or coverage["failed_count"]
    ):
        raise RuntimeError("source coverage counts disagree")
    added = set(coverage.get("newly_discovered", []))
    removed = set(coverage.get("disappeared", []))
    if not added <= ids or removed & ids:
        raise RuntimeError("source coverage delta disagrees with current pages")


def validate_tree(root: Path, strict: bool = False) -> None:
    required = [
        "state/sources.json",
        "state/source-coverage.json",
        "state/freshness.json",
        "state/practices.json",
        "state/sdk-python.json",
        "state/sdk-javascript.json",
        "state/skill.json",
        "BEST_PRACTICES.md",
    ]
    missing = [path for path in required if not (root / path).exists()]
    if missing:
        raise RuntimeError(f"missing generated files: {', '.join(missing)}")
    for path in (root / "state").glob("*.json"):
        json.loads(path.read_text())
    coverage = read_json(root / "state/source-coverage.json")
    validate_coverage(coverage)
    practices = read_json(root / "state/practices.json")
    for item in practices.get("items", []):
        if item["status"] == "recommended" and not item.get("sources"):
            raise RuntimeError(f"recommended practice lacks provenance: {item['id']}")
        for evidence in item.get("sources", []):
            if not all(evidence.get(key) for key in ("source_id", "url", "sha256", "excerpt")):
                raise RuntimeError(f"incomplete practice provenance: {item['id']}")
            if len(evidence["excerpt"]) > 420:
                raise RuntimeError(f"practice excerpt is too long: {item['id']}")
    sources = {
        item["source_id"]: item for item in read_json(root / "state/sources.json")["artifacts"]
    }
    if coverage.get("schema_version") == 2:
        for name in ("model-limitations", "cookbooks", "evaluation-tool"):
            if not (root / f"state/{name}.json").exists():
                raise RuntimeError(f"missing structured state: {name}")
    for name in (
        "model-limitations",
        "cookbooks",
        "evaluation-tool",
        "sdk-python",
        "sdk-javascript",
    ):
        value = read_json(root / f"state/{name}.json")
        if value is None:
            continue
        items = value.get("items", [value]) + value.get("capabilities", [])
        ids = [item["id"] for item in items if "id" in item]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"duplicate structured identity: {name}")
        for item in items:
            evidence = item.get("provenance", [])
            if not evidence:
                raise RuntimeError(f"missing structured provenance: {name}")
            for source in evidence:
                if not all(source.get(key) for key in ("source_id", "url", "sha256")):
                    raise RuntimeError(f"incomplete structured provenance: {name}")
                current = sources.get(source["source_id"])
                if item.get("status") != "unknown" and (
                    not current or source["sha256"] != current["sha256"]
                ):
                    raise RuntimeError(
                        f"structured provenance does not match retained artifact: {name}"
                    )
            if name == "model-limitations" and not item["id"].startswith(item["model"] + ":"):
                raise RuntimeError("limitation identity is not model-specific")
            if "introduced" in item and not all(
                item["source_commit"] in source["url"] for source in evidence
            ):
                raise RuntimeError("capability lacks immutable release provenance")
    limitations = read_json(root / "state/model-limitations.json")
    if limitations is not None and (
        root / "MODEL_LIMITATIONS.md"
    ).read_text() != render_limitations(limitations):
        raise RuntimeError("model limitations rendering disagrees with state")
    for path in (root / "events").glob("*.json"):
        data = read_json(path)
        ids = [event.get("id") for event in data.get("events", [])]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"duplicate event ID in {path}")
    if "state/practices.json" not in (root / "BEST_PRACTICES.md").read_text():
        raise RuntimeError("best-practices guide is not linked to generated state")


def promote(stage: Path, root: Path) -> None:
    for source in stage.rglob("*"):
        if source.is_dir():
            continue
        target = root / source.relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)


def synchronize(
    root: Path = ROOT,
    client: HTTPClient | None = None,
    strict: bool = False,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = client or HTTPClient()
    attempt = attempt if attempt is not None else {}
    attempt["phase"] = "discovery"
    previous_docs_manifest = read_json(root / "sources/docs.typesafe.ai/manifest.json", {}) or {}
    previous_github_manifest = (
        read_json(root / "sources/github.typesafe-ai/manifest.json", {}) or {}
    )
    previous_sources = read_json(root / "state/sources.json", {}) or {}
    previous_coverage = read_json(root / "state/source-coverage.json", {}) or {}
    previous_freshness = read_json(root / "state/freshness.json", {}) or {}
    previous_state = {
        name: read_json(root / f"state/{name}.json")
        for name in (
            "primitives",
            "api",
            "models",
            "practices",
            "sdk-python",
            "sdk-javascript",
            "skill",
            "model-limitations",
            "cookbooks",
            "evaluation-tool",
        )
    }
    try:
        sitemap_warning = None
        sitemap_lastmod: dict[str, str] = {}
        try:
            index_response = client.get(DOCS_INDEX)
            index = Artifact(
                "docs:llms.txt", "documentation-discovery", DOCS_INDEX, index_response.body
            )
            discovered = parse_llms(index_response.body.decode("utf-8"))
            if not discovered:
                raise FetchError("documentation index returned no same-host pages")
            discovery_method = "llms.txt"
            try:
                sitemap_response = client.get(DOCS_SITEMAP)
                sitemap_lastmod = parse_sitemap(sitemap_response.body.decode("utf-8"))
            except FetchError as exc:
                sitemap_warning = str(exc)
        except FetchError:
            # A different index cannot prove removals from the canonical llms index.
            raise
        if not discovered:
            raise FetchError("documentation discovery returned no same-host pages")
        intentionally_excluded = (
            parse_llms_exclusions(index_response.body.decode("utf-8"))
            if index.source_id == "docs:llms.txt"
            else []
        )
        current_ids = {docs_source_id(url) for url, _, _ in discovered}
        old_ids = {page["source_id"] for page in previous_coverage.get("pages", [])}
        attempt.update(
            {
                "phase": "fetch",
                "discovered_page_count": len(current_ids),
                "newly_discovered": sorted(current_ids - old_ids),
                "disappeared": sorted(old_ids - current_ids),
            }
        )
        artifacts: dict[str, Artifact] = {index.source_id: index}
        contents: dict[str, str] = {index.source_id: index.content.decode("utf-8")}
        descriptions: dict[str, tuple[str, str]] = {
            index.source_id: ("Documentation index", "Canonical documentation discovery")
        }
        for url, title, description in discovered:
            sid = docs_source_id(url)
            try:
                response = client.get(url if url.endswith(".md") else f"{url}.md")
            except FetchError:
                response = client.get(url)
            artifact = Artifact(
                sid,
                "documentation",
                response.url,
                response.body,
                index.source_id,
                sitemap_lastmod.get(docs_canonical_url(url)),
            )
            artifacts[sid] = artifact
            contents[sid] = response.body.decode("utf-8")
            descriptions[sid] = (title_from_markdown(contents[sid], title), description)
        skill_path = "/repos/typesafe-ai/skills/commits?path=skills/typesafe-ai/SKILL.md&per_page=1"
        skill_commits, skill_commit_response = github_json(client, skill_path)
        skill_commit = skill_commits[0]["sha"] if skill_commits else None
        skill_url = f"https://raw.githubusercontent.com/typesafe-ai/skills/{skill_commit}/skills/typesafe-ai/SKILL.md"
        skill_response = client.get(skill_url)
        skill_artifact = Artifact(
            "skill:typesafe-ai",
            "github-source",
            skill_url,
            skill_response.body,
            upstream_commit=skill_commit,
        )
        artifacts[skill_artifact.source_id] = skill_artifact
        contents[skill_artifact.source_id] = skill_response.body.decode("utf-8")
        artifacts["github:typesafe-ai/skills:commit"] = Artifact(
            "github:typesafe-ai/skills:commit",
            "github-api",
            f"{GITHUB_API}{skill_path}",
            evidence_bytes({"sha": skill_commit}),
        )
        python = sdk_snapshot(
            client,
            artifacts,
            "typesafe-ai/typesafe-sdk-python",
            "typesafe-sdk",
            "python",
            "pyproject.toml",
            contents.get("docs:sdk/python/changelog"),
            previous_state.get("sdk-python"),
        )
        javascript = sdk_snapshot(
            client,
            artifacts,
            "typesafe-ai/typesafe-sdk-js",
            "@typesafe-ai/sdk",
            "javascript",
            "package.json",
            contents.get("docs:sdk/javascript/changelog"),
            previous_state.get("sdk-javascript"),
        )
    except (ET.ParseError, FetchError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"sync aborted; last-known-good state was not changed: {exc}") from exc

    attempt["phase"] = "derivation"
    attempt["candidate_sdk_versions"] = {
        "python": python["version"],
        "javascript": javascript["version"],
    }
    state = derive_state(contents, artifacts, previous_state)
    state["model-limitations"] = observed_state(
        model_limitations(contents, artifacts, previous_state.get("model-limitations")),
        previous_state.get("model-limitations"),
    )
    state["cookbooks"] = observed_state(
        cookbooks(contents, artifacts, discovered, previous_state.get("cookbooks")),
        previous_state.get("cookbooks"),
    )
    state["evaluation-tool"] = evaluation_tool_snapshot(
        client, artifacts, previous_state.get("evaluation-tool")
    )
    state["sdk-python"] = python
    state["sdk-javascript"] = javascript
    observed = utc_now().isoformat().replace("+00:00", "Z")
    old_docs = {
        item["source_id"]: item
        for item in previous_docs_manifest.get("artifacts", [])
        if isinstance(item, dict) and item.get("source_id")
    }
    old_github = {
        item["source_id"]: item
        for item in previous_github_manifest.get("artifacts", [])
        if isinstance(item, dict) and item.get("source_id")
    }
    docs_items = [
        artifact_manifest(artifact, old_docs.get(sid), *descriptions.get(sid, ("", "")))
        for sid, artifact in sorted(artifacts.items())
        if artifact.source_type in {"documentation", "documentation-discovery"}
    ]
    github_items = [
        artifact_manifest(artifact, old_github.get(sid))
        for sid, artifact in sorted(artifacts.items())
        if artifact.source_type not in {"documentation", "documentation-discovery"}
    ]
    new_docs_manifest = {"schema_version": SCHEMA, "artifacts": docs_items}
    old_events_state = {name: previous_state.get(name) for name in state}
    baseline_exists = (root / "events/baseline.json").exists()
    candidates = (
        source_events(previous_docs_manifest, new_docs_manifest, observed)
        if baseline_exists
        else []
    )
    if baseline_exists:
        candidates.extend(semantic_events(old_events_state, state, observed))
    existing_ids = load_history_events(root / "events")
    new_events = deduplicate_events(candidates, existing_ids)
    attempt["candidate_event_count"] = len(new_events)
    attempt["candidate_event_counts"] = {
        category: sum(e["category"] == category for e in new_events)
        for category in sorted({e["category"] for e in new_events})
    }
    day = iso_day(utc_now())
    coverage = build_coverage(
        discovered,
        artifacts,
        index,
        discovery_method,
        sitemap_warning,
        previous_coverage,
        intentionally_excluded,
    )
    validate_coverage(coverage)
    discovery_delta = {key: coverage.pop(key) for key in ("newly_discovered", "disappeared")}
    # Attempt deltas belong in the result/diagnostics. Persisting them requires a
    # cleanup commit on the next identical run and violates idempotence.
    coverage["schema_version"] = 2
    freshness = observed_state(
        {
            "schema_version": SCHEMA,
            "docs": {"status": "up_to_date", "discovered_pages": len(discovered)},
            "python": {
                "status": "up_to_date" if not python["discrepancies"] else "discrepancy",
                "version": python["version"],
                "tag": python["source_tag"],
                "discrepancies": python["discrepancies"],
            },
            "javascript": {
                "status": "up_to_date" if not javascript["discrepancies"] else "discrepancy",
                "version": javascript["version"],
                "tag": javascript["source_tag"],
                "discrepancies": javascript["discrepancies"],
            },
            "skill": {"status": "observed", "commit": skill_commit},
            "grace_period_days": 3,
        },
        previous_freshness,
    )
    if freshness["fingerprint"] == previous_freshness.get("fingerprint"):
        freshness["last_successful_observation"] = previous_freshness.get(
            "last_successful_observation", observed
        )
    else:
        freshness["last_successful_observation"] = observed
    if strict:
        actionable = actionable_discrepancies(freshness, utc_now())
        if actionable:
            names = ", ".join(actionable)
            raise RuntimeError(
                f"strict sync failed; SDK release discrepancy persisted beyond grace period: {names}"
            )
    old_source_items = {
        item["source_id"]: item
        for item in previous_sources.get("artifacts", [])
        if isinstance(item, dict) and item.get("source_id")
    }
    sources = {
        "schema_version": SCHEMA,
        "scope": "official TypeSafe public sources only",
        "documentation_license_note": "Hosted documentation is observed by metadata, hashes, and short excerpts; it is not bulk mirrored or relicensed.",
        "artifacts": sorted(
            [
                artifact_manifest(
                    artifact, old_source_items.get(sid), *descriptions.get(sid, ("", ""))
                )
                for sid, artifact in artifacts.items()
            ],
            key=lambda item: item["source_id"],
        ),
    }
    stage = Path(tempfile.mkdtemp(prefix="jev-docs-stage-", dir=root.parent))
    try:
        write_json(
            stage / "sources/docs.typesafe.ai/manifest.json",
            {"schema_version": SCHEMA, "source": DOCS_ROOT, "artifacts": docs_items},
        )
        write_json(
            stage / "sources/github.typesafe-ai/manifest.json",
            {
                "schema_version": SCHEMA,
                "source": "https://github.com/typesafe-ai",
                "artifacts": github_items,
            },
        )
        for name, value in {
            "sources": sources,
            "source-coverage": coverage,
            "freshness": freshness,
            **state,
        }.items():
            write_json(stage / f"state/{name}.json", value)
        (stage / "MODEL_LIMITATIONS.md").write_text(render_limitations(state["model-limitations"]))
        (stage / "BEST_PRACTICES.md").write_text(render_best_practices(state["practices"]))
        events_dir = stage / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        if not (root / "events/baseline.json").exists():
            write_json(
                events_dir / "baseline.json",
                {
                    "schema_version": SCHEMA,
                    "kind": "baseline",
                    "observed_at": observed,
                    "note": "Initial canonical snapshot; not a list of additions.",
                    "source_count": len(artifacts),
                    "documentation_page_count": len(discovered),
                },
            )
        existing_day_events: list[dict[str, Any]] = []
        existing_day_path = root / f"events/{day}.json"
        if existing_day_path.exists():
            existing_day_events = read_json(existing_day_path, {}).get("events", [])
        all_day_events = rollup_events(existing_day_events, new_events)
        if new_events:
            write_json(
                events_dir / f"{day}.json",
                {"schema_version": SCHEMA, "date": day, "events": all_day_events},
            )
            (stage / "changes").mkdir(parents=True, exist_ok=True)
            (stage / f"changes/{day}.md").write_text(render_daily_report(day, all_day_events))
        attempt["phase"] = "validation"
        validate_tree(stage, strict=True)
        attempt["phase"] = "promotion"
        promote(stage, root)
        attempt["phase"] = "sync-complete"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {
        "pages": len(discovered),
        "artifacts": len(artifacts),
        "practices": len(state["practices"]["items"]),
        "events": len(new_events),
        "day": day,
        **discovery_delta,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--strict", action="store_true")
    sync_parser.add_argument(
        "--diagnostics", type=Path, help="Write attempt metadata outside canonical state"
    )
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    attempt = {
        "attempt_timestamp": utc_now().isoformat().replace("+00:00", "Z"),
        "result": "failure",
    }
    try:
        if args.command == "sync":
            print(
                json.dumps(synchronize(ROOT, strict=args.strict, attempt=attempt), sort_keys=True)
            )
            attempt["result"] = "success"
        else:
            validate_tree(ROOT, strict=args.strict)
            print("validation passed")
    except (RuntimeError, FetchError) as exc:
        attempt["result"] = "failure"
        # Do not persist arbitrary exception strings, response bodies or local paths.
        attempt["error_type"] = type(exc).__name__
        print(f"jev-docs: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.command == "sync" and args.diagnostics:
            write_json(args.diagnostics, attempt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
