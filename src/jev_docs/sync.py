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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jev_docs.api import extract_api, validate_api
from jev_docs.inventories import cookbooks, model_limitations, render_limitations
from jev_docs.practice_reviews import (
    PRACTICE_EVIDENCE_POLICY,
    REVIEWED_PRACTICE_CONTEXTS,
    practice_context,
)
from jev_docs.text import clean_text, excerpt, markdown_tokens, without_code_examples
from jev_docs.transactions import PENDING, promote, recover_pending, synchronization_lock

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# datetime.UTC requires Python 3.11; the project supports Python 3.10.
UTC = timezone.utc

ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = "https://docs.typesafe.ai"
DOCS_INDEX = f"{DOCS_ROOT}/llms.txt"
DOCS_SITEMAP = f"{DOCS_ROOT}/sitemap.xml"
GITHUB_API = "https://api.github.com"
USER_AGENT = "jev-docs-sync/0.1 (+https://github.com/chenrui333/jev-docs)"
SCHEMA = 1
GITHUB_SOURCE_ROOT = "https://github.com/typesafe-ai"
SOURCE_SCOPE = "official TypeSafe public sources only"
DOCUMENTATION_LICENSE_NOTE = (
    "Hosted documentation is observed by metadata, hashes, and short excerpts; "
    "it is not bulk mirrored or relicensed."
)
DOCS_SOURCE_TYPES = frozenset(
    {"documentation", "documentation-discovery", "historical-documentation"}
)


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
    """A bounded public-source failure with a safe message and machine-readable code."""

    def __init__(
        self, message: str, status: int | None = None, *, code: str = "fetch_failed"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def https_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or 443
    except ValueError:
        return None


def source_label(url: str) -> str:
    """Never include userinfo or query parameters in public-source errors."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


class SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib normally copies Authorization before sending a redirected request.
        # Reject the target here, not merely after its response has been received.
        if https_origin(newurl) is None or https_origin(newurl) != https_origin(req.full_url):
            raise FetchError("refusing cross-origin redirect", code="unsafe_redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return min(float(value), 60.0)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return min(max(date.timestamp() - time.time(), 0.0), 60.0)
    except (ValueError, TypeError, OverflowError):
        return None


class HTTPClient:
    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 2,
        timeout: float = 20.0,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.opener = opener or build_opener(SameOriginRedirectHandler()).open
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def get(self, url: str) -> Response:
        origin = https_origin(url)
        if origin is None:
            raise FetchError(
                "refusing non-HTTPS or credential-bearing source URL", code="unsafe_url"
            )
        last_error = "transport failure"
        status: int | None = None
        for attempt in range(self.retries + 1):
            delay = 0.25 * (2**attempt)
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
                token = os.environ.get("GITHUB_TOKEN")
                if origin == ("https", "api.github.com", 443) and token:
                    request.add_unredirected_header("Authorization", f"Bearer {token}")
                with self.opener(request, timeout=self.timeout) as raw:
                    body = raw.read(self.max_response_bytes + 1)
                    if len(body) > self.max_response_bytes:
                        raise FetchError(
                            f"response exceeds {self.max_response_bytes} bytes for {source_label(url)}",
                            code="response_too_large",
                        )
                    headers = {key.lower(): value for key, value in raw.headers.items()}
                    final_url = str(raw.url)
                    response_status = getattr(raw, "status", 200)
            except HTTPError as exc:
                status = exc.code
                headers = {key.lower(): value for key, value in exc.headers.items()}
                exc.close()
                last_error = f"HTTP {status}"
                retry_after = retry_after_seconds(headers.get("retry-after"))
                primary_limit = (
                    origin == ("https", "api.github.com", 443)
                    and headers.get("x-ratelimit-remaining") == "0"
                )
                github_limit = origin == ("https", "api.github.com", 443) and (
                    primary_limit or retry_after is not None
                )
                if status < 500 and status != 429 and not (status == 403 and github_limit):
                    break
                if retry_after is not None:
                    delay = retry_after
                elif status in {403, 429} and primary_limit:
                    reset = headers.get("x-ratelimit-reset", "")
                    if re.fullmatch(r"\d+", reset):
                        delay = min(max(float(reset) - time.time(), 0.0), 60.0)
            except ValueError:
                raise FetchError(
                    "invalid source request configuration", code="invalid_request"
                ) from None
            except (OSError, URLError, HTTPException):
                # Truncated bodies and malformed status lines are transport failures.
                # Exception messages/chains may contain credentials or response data.
                last_error = "transport failure"
                status = None
            else:
                if https_origin(final_url) != origin:
                    # Also enforce attribution for injected fixture/custom openers.
                    raise FetchError("refusing cross-origin redirect", code="unsafe_redirect")
                return Response(final_url, body, headers, response_status)
            if attempt < self.retries:
                self.sleep(delay)
        raise FetchError(f"failed to fetch {source_label(url)}: {last_error}", status) from None


class _ObservedClient:
    """Keep per-attempt HTTP success separate from later evidence interpretation."""

    def __init__(self, client, attempt):
        self.client = client
        self.attempt = attempt

    def get(self, url):
        response = self.client.get(url)
        self.attempt["last_successful_fetch"] = utc_now().isoformat().replace("+00:00", "Z")
        return response


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


def _bounded_json_loads(value: str | bytes) -> Any:
    """Bound container nesting before decoding, independent of Python's recursion limit."""
    if isinstance(value, bytes):
        value = value.decode(json.detect_encoding(value), "surrogatepass")
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON container nesting exceeds 64")
        elif character in "]}":
            depth -= 1
    return json.loads(value)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return _bounded_json_loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError):
        # Persisted evidence may be corrupt; never echo its body or machine path.
        parts = path.parts
        start = next(
            (
                index
                for index in range(len(parts) - 1, -1, -1)
                if parts[index] in {"state", "sources", "events"}
            ),
            len(parts) - 1,
        )
        label = Path(*parts[start:]).as_posix()
        raise RuntimeError(f"cannot read persisted JSON: {label}") from None


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
        if https_origin(url) != ("https", "docs.typesafe.ai", 443) or url in seen:
            continue
        seen.add(url)
        results.append((url, title.strip(), (description or "").strip()))
    return results


def parse_llms_exclusions(text: str, base: str = DOCS_ROOT) -> list[dict[str, str]]:
    """Record links excluded because they are not HTTPS documentation sources."""
    exclusions: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"-\s*\[([^]]+)\]\(([^)]+)\)", text):
        title, raw_url = match.groups()
        url = normalize_url(raw_url, base)
        if https_origin(url) != ("https", "docs.typesafe.ai", 443):
            reason = "non-https" if urlsplit(url).scheme != "https" else "external-host"
            exclusions[url] = {"url": url, "title": title.strip(), "reason": reason}
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
        url = docs_canonical_url(loc.strip())
        if https_origin(url) == ("https", "docs.typesafe.ai", 443):
            result[url] = (lastmod or "").strip()
    return result


def title_from_markdown(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    return re.sub(r"[*`]", "", match.group(1)).strip() if match else fallback


# Each claim pairs a reviewed normalized block hash with a short supporting
# excerpt. Every claim must match in one allowed source. Hashes keep unreviewed
# negations/context changes unknown without embedding entire upstream paragraphs.
PRACTICE_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "jev-complements-generative-agents",
        "category": "agent-integration",
        "summary": "Use coding agents to write software that calls Jev for bounded structured decisions such as routing, classification, scoring and guardrails. Jev does not replace their generative base model or write code or converse.",
        "claims": [
            (
                "685ee84913021250f4ca81c1f16e0cb87557aec1687a49e33181136a04c24613",
                "Instead, you can use your coding agent as usual to write code that uses Jev to make decisions.",
            ),
            (
                "685ee84913021250f4ca81c1f16e0cb87557aec1687a49e33181136a04c24613",
                "Jev is not a drop-in replacement for the LLM behind Claude Code, Cursor, opencode, Copilot, Muse Spark, Grok Bot, or similar tools.",
            ),
            (
                "72551aa8c71e1084cf95db4849b3f07f697494c8233f32cc6550521b2c3386e0",
                "It does not generate text, write code, or hold a conversation.",
            ),
            (
                "72551aa8c71e1084cf95db4849b3f07f697494c8233f32cc6550521b2c3386e0",
                "It takes a state and a set of typed questions and returns structured answers your code can use directly:",
            ),
            (
                "dc2d14f3619aba5159cbaae9394212408d812aa05c328ddcb5da90b2b52d2067",
                "Use Jev inside an app or agent you're building — for routing, classification, scoring, guardrails, or any structured decision",
            ),
        ],
        "sources": ["docs:introduction/coding-agents"],
    },
    {
        "id": "prefer-typed-decisions-over-prompt-parsing",
        "category": "agent-integration",
        "summary": "For decisions expressible as TypeSafe's typed questions, consider replacing fragile generative return-JSON prompts with typed decision calls. This guidance does not cover arbitrary text generation or every JSON task.",
        "claims": [
            (
                "c1505a798fa78f4503344c7dfd2d68e6c8d800599ebdfd7e3103d82dcf1654f9",
                'Replace a fragile prompt that asks an LLM to "return JSON" with a call that returns typed values by construction.',
            ),
            (
                "e5e4c9b75f7cba22097e1b679ab1bdf7bd88c1bc25a37418261214d3460506ae",
                "If any of that matches what you're building, the fastest path in is the Quick start, then the primitives reference for the question types.",
            ),
        ],
        "sources": ["docs:introduction/coding-agents"],
    },
    {
        "id": "deterministic-before-jev",
        "category": "decision-boundary",
        "summary": "Keep deterministic rules, calculations, exact lookups, control flow, and side effects in code; use Jev where semantic judgment is needed.",
        "claims": [
            (
                "740b9bc462a4ad655387589dc0d09fd5754ebc0b545c36dfc62d42597941b4c8",
                "Keep known rules, calculations, exact lookups, and execution in code.",
            ),
            (
                "740b9bc462a4ad655387589dc0d09fd5754ebc0b545c36dfc62d42597941b4c8",
                "Preserve the user's chosen stack and scope; add TypeSafe where semantic understanding helps.",
            ),
            (
                "9bdf98323be29d02cffdc2272ea699f79101c692e30a72fe2bf7aec21d6e8cf5",
                "Code owns the workflow; the model supplies programmable common sense where ordinary code needs semantic understanding.",
            ),
        ],
        "sources": ["docs:concepts/how-to-build-with-system-one", "skill:typesafe-ai"],
    },
    {
        "id": "narrow-coherent-questions",
        "category": "question-design",
        "summary": "Ask narrow, coherent, typed questions with explicit instructions and criteria rather than hiding several judgments in one broad question.",
        "claims": [
            (
                "efe294ba28f0b1f156db8f4001169d0c59f6424881c182aa8ab38197e9915a50",
                "Break broad judgments into narrow, typed questions with explicit instructions and criteria.",
            ),
        ],
        "sources": ["docs:concepts/how-to-build-with-system-one", "skill:typesafe-ai"],
    },
    {
        "id": "batch-independent-questions",
        "category": "batching",
        "summary": "Ask independent questions over the same state together so the model can evaluate them in parallel and code can compose the answers.",
        "claims": [
            (
                "a70670a056de5ce7c8787edf3e0fd4fd64aa450ab17ff3abacb9df1223471195",
                "Ask independent questions over the same state together, including useful speculative questions.",
            ),
            (
                "a70670a056de5ce7c8787edf3e0fd4fd64aa450ab17ff3abacb9df1223471195",
                "They run in parallel and cannot see one another's answers.",
            ),
            (
                "a70670a056de5ce7c8787edf3e0fd4fd64aa450ab17ff3abacb9df1223471195",
                "State each speculative premise explicitly; code consumes the applicable answers.",
            ),
        ],
        "sources": ["docs:concepts/state", "docs:patterns/fan-out", "skill:typesafe-ai"],
    },
    {
        "id": "explicit-speculative-premises",
        "category": "batching",
        "summary": "Speculative questions are acceptable in a batch, but their premises must be stated explicitly and code must decide which answers are relevant.",
        "claims": [
            (
                "a70670a056de5ce7c8787edf3e0fd4fd64aa450ab17ff3abacb9df1223471195",
                "Ask independent questions over the same state together, including useful speculative questions.",
            ),
            (
                "a70670a056de5ce7c8787edf3e0fd4fd64aa450ab17ff3abacb9df1223471195",
                "State each speculative premise explicitly; code consumes the applicable answers.",
            ),
        ],
        "sources": ["docs:patterns/fan-out", "skill:typesafe-ai"],
    },
    {
        "id": "choice-confidence-concentration",
        "category": "confidence",
        "summary": "Choice confidence summarizes concentration of the competing option probabilities; it is not a guarantee that the selected answer is correct.",
        "claims": [
            (
                "929b3bc1c6930cc43bfee8d1eef11a63520068cc5c5c987b8a07ecea0f212a0e",
                "Choice/Score confidence summarizes distribution concentration, not overall workflow correctness or permission to act.",
            ),
        ],
        "sources": ["docs:confidence", "skill:typesafe-ai"],
    },
    {
        "id": "confidence-is-not-permission",
        "category": "confidence",
        "summary": "Confidence is an uncertainty signal. Keep the permission to act, review, or escalate explicit in application code.",
        "claims": [
            (
                "a0d4f9a3c2b7e7b3276c1dd2e561f136a3d648abe84fbaec00aed05f32189bf6",
                "The 0.5 confidence floor catches anything the model reports as genuinely uncertain.",
            ),
            (
                "a0d4f9a3c2b7e7b3276c1dd2e561f136a3d648abe84fbaec00aed05f32189bf6",
                "Your code encodes the risk tolerance.",
            ),
            (
                "6d65a27cca23d321bc57ce72cb3ccf753349d15c8a95acdba6abcd483e7e31a7",
                "Depending on context, you might ask the user to confirm, flag for review, or gather more information before acting.",
            ),
            (
                "0b7b3adcb105f380094b642b38ba9fab4cf2479d85d113103683b713169de6fa",
                "Route to a human, request clarification, or fall back to a different system.",
            ),
        ],
        "sources": ["docs:confidence", "skill:typesafe-ai"],
    },
    {
        "id": "noul-is-yes-probability",
        "category": "primitives",
        "summary": "A Noul value is the probability that a yes/no proposition is true; a value near 0.5 means uncertainty between yes and no, not medium intensity.",
        "claims": [
            (
                "fa3fcf6dca7b43340375a0b3f1186c99eaa5940e3e22049318859a816a91ebb3",
                "It is the probability that the answer is yes.",
            ),
            (
                "fa3fcf6dca7b43340375a0b3f1186c99eaa5940e3e22049318859a816a91ebb3",
                "If the question is really about degree, the value does not measure the degree.",
            ),
            (
                "bfcc615d1a13a12730b1748fc887372e2c24672cabc35ec52c22d7861a4af652",
                "A value near 0.5 means the model gives yes and no similar probability.",
            ),
        ],
        "sources": ["docs:primitives/noul", "skill:typesafe-ai"],
    },
    {
        "id": "candidate-must-be-present",
        "category": "question-design",
        "summary": "Candidate selection questions can only select a value that is actually present in the candidate state or criteria.",
        "claims": [
            (
                "1c0e6728e3de699b8c8d93e94c62e527bc9c13ed97a60e3a4c94889f4c5eccea",
                "For source-value selection, check candidate coverage: the model cannot choose an omitted value.",
            ),
        ],
        "sources": ["skill:typesafe-ai", "docs:primitives/choice"],
    },
    {
        "id": "domain-calibrated-thresholds",
        "category": "thresholds",
        "summary": "Choose thresholds from the application's data, model performance, and consequences; do not treat cookbook thresholds as universal defaults.",
        "claims": [
            (
                "929b3bc1c6930cc43bfee8d1eef11a63520068cc5c5c987b8a07ecea0f212a0e",
                "Use probabilities and confidence to guide behavior, with thresholds evaluated on the user's data and consequences.",
            ),
            (
                "74b58751ea78857d31d8762c2ced8771ee37e8db261f839ee9db714063fdbe09",
                "System One models are trained for calibrated decisions; validate their performance in the target domain.",
            ),
            (
                "4aabfceeaa73a471af8b53b87d88ba5c9e34f24487fc6ab8c2154fe290d36475",
                "Treat cookbook thresholds and demo results as examples to evaluate, not universal rules or permanent model limitations.",
            ),
        ],
        "sources": ["docs:confidence", "skill:typesafe-ai"],
    },
    {
        "id": "typed-output-is-not-truth",
        "category": "verification",
        "summary": "Typed output guarantees interface shape, not truth; test representative cases and resulting application behavior.",
        "claims": [
            (
                "74b58751ea78857d31d8762c2ced8771ee37e8db261f839ee9db714063fdbe09",
                "Typed output guarantees the interface, not truth.",
            ),
            (
                "4aabfceeaa73a471af8b53b87d88ba5c9e34f24487fc6ab8c2154fe290d36475",
                "Test representative cases and the resulting application behavior.",
            ),
        ],
        "sources": ["docs:concepts/system-one", "skill:typesafe-ai"],
    },
    {
        "id": "observed-state-distinct-from-inference",
        "category": "state",
        "summary": "Keep observed application state and inferred model judgments conceptually distinct, and check freshness before applying a result to changed state.",
        "claims": [
            (
                "6d1f782682e7f849ea99ec4df1f84a91e6161dfc958e68e4d2c3bf7b98fe3f99",
                "Keep inferred state distinct from observed facts, and check freshness before applying a result to a changed situation.",
            ),
        ],
        "sources": ["docs:concepts/state", "skill:typesafe-ai"],
    },
    {
        "id": "diagnose-failure-layers",
        "category": "verification",
        "summary": "For failures, inspect the exact state, questions, candidates, answers, composition, and observed outcome; separate evidence, model, code, and service failures.",
        "claims": [
            (
                "4aabfceeaa73a471af8b53b87d88ba5c9e34f24487fc6ab8c2154fe290d36475",
                "For failures, inspect the exact state, questions, candidates, answers, composition, and observed outcome.",
            ),
            (
                "4aabfceeaa73a471af8b53b87d88ba5c9e34f24487fc6ab8c2154fe290d36475",
                "Separate missing evidence, model errors, code errors, and service failures.",
            ),
        ],
        "sources": ["skill:typesafe-ai", "docs:concepts/how-to-build-with-system-one"],
    },
)


def rule_evidence(
    rule: dict[str, Any], contents: dict[str, str], artifact_by_id: dict[str, Artifact]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require reviewed complete context and all literal claims in one source."""
    evidence: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    uncertain = False
    for source_id in rule["sources"]:
        content, artifact = contents.get(source_id), artifact_by_id.get(source_id)
        if content is None or artifact is None:
            verdict = "unavailable" if content is None and artifact is None else "unreviewed"
            reviews.append({"source_id": source_id, "verdict": verdict})
            uncertain = uncertain or verdict == "unreviewed"
            continue
        if content != artifact.content.decode():
            reviews.append({"source_id": source_id, "verdict": "unreviewed"})
            uncertain = True
            continue
        context, context_hash = practice_context(content)
        review = REVIEWED_PRACTICE_CONTEXTS.get(source_id, {}).get(context_hash, {})
        verdict = review.get("verdicts", {}).get(rule["id"], "unreviewed")
        observation = {
            "source_id": source_id,
            "url": artifact.url,
            "sha256": artifact.sha256,
            "context_sha256": context_hash,
            "verdict": verdict,
        }
        if review:
            observation["review_source"] = {
                "url": review["source_url"],
                "sha256": review["source_sha256"],
            }
        reviews.append(observation)
        if verdict == "non_conflicting":
            continue
        if verdict != "supported":
            uncertain = True
            continue
        prose = re.sub(r"\n(?=\s*\|)", "\n\n", clean_text(without_code_examples(context)))
        tokens = markdown_tokens(prose)
        blocks = {}
        for token in tokens or []:
            if token.type == "inline":
                block = re.sub(r"\s+", " ", clean_text(token.content)).strip()
                blocks[sha256_bytes(block.encode())] = block
        if tokens is None or not all(
            claim in blocks.get(digest, "") and len(claim) <= 420
            for digest, claim in rule["claims"]
        ):
            observation["verdict"] = "unverified_passages"
            uncertain = True
            continue
        evidence.extend(
            {
                "source_id": source_id,
                "url": artifact.url,
                "sha256": artifact.sha256,
                "context_sha256": context_hash,
                "excerpt": claim,
            }
            for _digest, claim in rule["claims"]
        )
    return ([] if uncertain else evidence), reviews


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


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def sdk_freshness(
    snapshot: dict[str, Any], previous: dict[str, Any], previous_freshness: dict[str, Any]
) -> dict[str, Any]:
    entry = {
        "status": "discrepancy" if snapshot["discrepancies"] else "up_to_date",
        "version": snapshot["version"],
        "tag": snapshot["source_tag"],
        "discrepancies": snapshot["discrepancies"],
    }
    if entry["status"] == "discrepancy":
        # The grace period belongs to this SDK's continuous discrepancy, not to the
        # whole freshness snapshot: unrelated docs, skill or other-SDK changes must
        # not restart it. Older snapshots lack the field; their snapshot timestamp is
        # the best retained evidence of when the discrepancy was already present.
        first = None
        if previous.get("status") == "discrepancy":
            first = previous.get("discrepancy_first_observed") or previous_freshness.get(
                "observed_at"
            )
        entry["discrepancy_first_observed"] = first or utc_now().isoformat().replace("+00:00", "Z")
    return entry


def actionable_discrepancies(freshness: dict[str, Any], now: datetime) -> list[str]:
    grace_days = freshness.get("grace_period_days", 3)
    if not isinstance(grace_days, int):
        return []
    actionable = []
    for name in ("python", "javascript"):
        entry = freshness.get(name, {})
        if entry.get("status") != "discrepancy":
            continue
        first_seen = parse_utc(
            entry.get("discrepancy_first_observed") or freshness.get("observed_at")
        )
        if first_seen and now - first_seen >= timedelta(days=grace_days):
            actionable.append(name)
    return actionable


def artifact_manifest(
    artifact: Artifact, previous: dict[str, Any] | None, title: str = "", description: str = ""
) -> dict[str, Any]:
    same = previous and previous.get("sha256") == artifact.sha256
    # Sitemap lastmod moves on site redeploys and sitemap outages without content
    # changes. While the content hash is unchanged, keep the lastmod recorded with
    # that content, including an absent one.
    last_modified = (
        previous.get("upstream_last_modified") if same else artifact.upstream_last_modified
    )
    return {
        "source_id": artifact.source_id,
        "source_type": artifact.source_type,
        "canonical_url": docs_canonical_url(artifact.url)
        if artifact.url.startswith(DOCS_ROOT)
        else artifact.url,
        "fetch_url": artifact.url,
        "discovered_via": artifact.discovered_via,
        "upstream_last_modified": last_modified,
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


def metadata_json(response: Response, service: str) -> Any:
    try:
        return _bounded_json_loads(response.body)
    except (ValueError, RecursionError):
        raise FetchError(
            f"{service} metadata JSON invalid at {source_label(response.url)}; "
            "check upstream availability and retry; no evidence promoted",
            code="invalid_json",
        ) from None


def schema_error(response: Response, service: str, expected: str) -> FetchError:
    return FetchError(
        f"{service} metadata schema invalid at {source_label(response.url)}; "
        f"expected {expected}; check upstream schema before retrying",
        code="invalid_schema",
    )


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_github_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def package_names_match(observed: Any, expected: Any, package_kind: str) -> bool:
    if not nonempty_string(observed) or not nonempty_string(expected):
        return False
    if package_kind == "python":
        # PEP 503 canonicalizes case and each run of '-', '_' and '.'.
        return re.sub(r"[-_.]+", "-", observed).lower() == re.sub(r"[-_.]+", "-", expected).lower()
    return observed == expected


def registry_json(response: Response, package_kind: str, package: str) -> dict[str, Any]:
    service = "PyPI" if package_kind == "python" else "npm"
    value = metadata_json(response, service)
    if not isinstance(value, dict):
        raise schema_error(response, service, "a JSON object")
    if package_kind == "python":
        info, releases = value.get("info"), value.get("releases")
        if not isinstance(info, dict) or not nonempty_string(info.get("version")):
            raise schema_error(response, service, "info.version as a nonempty string")
        if not isinstance(releases, dict) or not all(
            isinstance(files, list) for files in releases.values()
        ):
            raise schema_error(response, service, "releases as a version-to-file-list map")
        if info["version"] not in releases:
            raise schema_error(response, service, "info.version to appear in the releases map")
        if not package_names_match(info.get("name"), package, package_kind):
            raise schema_error(response, service, "info.name matching the requested package")
    else:
        tags = value.get("dist-tags")
        if not isinstance(tags, dict) or not nonempty_string(tags.get("latest")):
            raise schema_error(response, service, "dist-tags.latest as a nonempty string")
        if not package_names_match(value.get("name"), package, package_kind):
            raise schema_error(response, service, "name matching the requested scoped package")
    return value


def validate_github_metadata(value: Any, response: Response) -> None:
    endpoint = urlsplit(response.url).path
    match = re.fullmatch(r"/repos/[^/]+/[^/]+/(tags|releases|commits)", endpoint)
    surface = match[1] if match else None
    if surface in {"tags", "releases", "commits"}:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise schema_error(response, "GitHub", f"{surface} as a list of objects")
        if surface == "commits":
            if not value or any(not is_github_commit_sha(item.get("sha")) for item in value):
                raise schema_error(
                    response, "GitHub", "a nonempty commits list with 40-hex sha strings"
                )
        elif surface == "tags":
            if any(
                not nonempty_string(item.get("name"))
                or not isinstance(item.get("commit"), dict)
                or not nonempty_string(item["commit"].get("sha"))
                for item in value
            ):
                raise schema_error(response, "GitHub", "tag name and commit.sha strings")
        else:
            if any(
                not nonempty_string(item.get("tag_name"))
                or any(
                    item.get(key) is not None and not isinstance(item[key], str)
                    for key in ("published_at", "name", "html_url")
                )
                or any(
                    key in item and not isinstance(item[key], bool)
                    for key in ("draft", "prerelease")
                )
                for item in value
            ):
                raise schema_error(
                    response, "GitHub", "release tag strings and typed release fields"
                )
    elif not isinstance(value, dict):
        raise schema_error(response, "GitHub", "a JSON object")
    elif "/commits/" in endpoint:
        if not is_github_commit_sha(value.get("sha")):
            raise schema_error(response, "GitHub", "commit.sha as a 40-hex string")
    elif "default_branch" in value and not nonempty_string(value["default_branch"]):
        raise schema_error(response, "GitHub", "default_branch as a nonempty string")


def github_next_page(response: Response, initial_url: str) -> str | None:
    links = next((v for k, v in response.headers.items() if k.lower() == "link"), "")
    next_url = None
    for link in links.split(",") if links else []:
        match = re.fullmatch(r"\s*<([^>]+)>\s*;(.*)", link)
        relation = re.search(r'\brel\s*=\s*"?([^";]+)', match[2]) if match else None
        if not match or not relation:
            raise FetchError("invalid GitHub pagination Link header", code="invalid_pagination")
        if "next" not in relation[1].strip().split():
            continue
        candidate = urljoin(response.url, match[1])
        if (
            next_url is not None
            or https_origin(candidate) != ("https", "api.github.com", 443)
            or urlsplit(candidate).path != urlsplit(initial_url).path
            or urlsplit(candidate).fragment
        ):
            raise FetchError("unsafe GitHub pagination target", code="invalid_pagination")
        next_url = candidate
    return next_url


def github_json(client: HTTPClient, path: str) -> tuple[Any, Response]:
    initial_url = f"{GITHUB_API}{path}"
    match = re.fullmatch(r"/repos/[^/]+/[^/]+/(tags|releases)", urlsplit(initial_url).path)
    surface = match[1] if match else None
    paginated = surface is not None
    url, visited, items = initial_url, set(), {}
    first_response = None
    for page in range(20):
        visited.add(url)
        response = client.get(url)
        if first_response is None:
            first_response = response
        value = metadata_json(response, "GitHub")
        validate_github_metadata(value, response)
        if not paginated:
            return value, response
        projection = stable_tag_evidence if surface == "tags" else stable_release_evidence
        for item in value:
            # Deduplicate projected evidence, not volatile counters/download URLs.
            key = canonical_json(projection([item])[0])
            items.setdefault(key, item)
        next_url = github_next_page(response, initial_url)
        if next_url is None:
            return [items[key] for key in sorted(items)], first_response
        if next_url in visited or page == 19:
            raise FetchError(
                "GitHub pagination incomplete: cycle or 20-page limit; no evidence promoted",
                code="pagination_limit",
            )
        url = next_url
    raise AssertionError("unreachable pagination state")


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
            {
                **{key: item.get(key) for key in ("tag_name", "published_at", "name", "html_url")},
                "draft": item.get("draft", False),
                "prerelease": item.get("prerelease", False),
            }
            if isinstance(item, dict)
            else {"malformed": True}
            for item in value
        ),
        key=canonical_json,
    )


def stable_head_evidence(value: Any) -> dict[str, Any]:
    return {"sha": value.get("sha")} if isinstance(value, dict) else {"malformed": True}


def parse_simple_toml_value(text: str, key: str) -> str | None:
    """Read a static PEP 621 project string; never infer root/tool-table fields."""
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(project, dict):
        return None
    dynamic = project.get("dynamic", [])
    if (
        not isinstance(dynamic, list)
        or any(not isinstance(field, str) for field in dynamic)
        or key in dynamic
    ):
        return None
    value = project.get(key)
    return value if nonempty_string(value) else None


STABLE_VERSION_PATTERN = (
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def parse_release_notes(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    text = re.sub(r"<h2\b[^>]*>\s*(.*?)\s*</h2>", r"## \1", text, flags=re.S)
    headings = list(
        re.finditer(
            rf"^\s*#+\s*v?(?P<version>{STABLE_VERSION_PATTERN})\s*\((?P<date>[^)]+)\)",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
    )
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[match.end() : end]
        bullets = [
            re.sub(r"\s+", " ", line.strip()[2:].strip())
            for line in block.splitlines()
            if line.strip().startswith(("-", "*"))
        ]
        result.append({"version": match["version"], "date": match["date"], "notes": bullets[:12]})
    return result


def version_tuple(value: Any) -> tuple[int, ...] | None:
    """Return stable SemVer precedence; build metadata does not affect ordering."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(rf"v?{STABLE_VERSION_PATTERN}", value.strip())
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
        if not isinstance(entry, dict) or not nonempty_string(entry.get("name")):
            malformed.append(f"tag_entry_{index}_malformed")
            continue
        commit = entry.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            malformed.append(f"tag_entry_{index}_missing_commit")
            continue
        if not is_github_commit_sha(sha):
            malformed.append(f"tag_entry_{index}_invalid_commit")
            continue
        if entry["name"] in tag_map and tag_map[entry["name"]] != sha:
            malformed.append(f"tag_entry_{index}_conflicting_commit")
        tag_map[entry["name"]] = sha
    release_records: list[dict[str, Any]] = []
    release_statuses: dict[str, tuple[bool, bool]] = {}
    excluded_tags: set[str] = set()
    if not isinstance(releases, list):
        malformed.append("releases_not_a_list")
        releases = []
    for index, entry in enumerate(releases):
        if (
            not isinstance(entry, dict)
            or not nonempty_string(entry.get("tag_name"))
            or any(
                entry.get(key) is not None and not isinstance(entry[key], str)
                for key in ("published_at", "name", "html_url")
            )
            or any(
                key in entry and not isinstance(entry[key], bool) for key in ("draft", "prerelease")
            )
        ):
            malformed.append(f"release_entry_{index}_malformed")
            continue
        tag = entry["tag_name"]
        status = (entry.get("draft", False), entry.get("prerelease", False))
        if tag in release_statuses and release_statuses[tag] != status:
            malformed.append(f"release_entry_{index}_conflicting_status")
        release_statuses[tag] = status
        # Naming outside stable SemVer is unresolved, not a corrupt JSON record.
        # This includes prereleases with optional build metadata and custom tags.
        if (
            version_tuple(tag) is None
            or entry.get("draft") is True
            or entry.get("prerelease") is True
        ):
            excluded_tags.add(tag)
            continue
        release_records.append(entry)
    expected_tag = f"v{version}" if version_tuple(version) else None
    if expected_tag is None:
        discrepancies.append("registry_version_unresolved")
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
    ] + [
        version_tuple(name) for name in tag_map if name not in excluded_tags and version_tuple(name)
    ]
    if registry_version and source_versions and max(source_versions) > registry_version:
        discrepancies.append("git_source_ahead_of_registry")
    if registry_version and source_versions and registry_version > max(source_versions):
        discrepancies.append("registry_ahead_of_git_source")
    if malformed:
        discrepancies.append("malformed_release_metadata")
    resolved = bool(expected_tag in tag_map and matching_release is not None and not malformed)
    return {
        "source_tag": expected_tag if resolved else None,
        "source_commit": tag_map[expected_tag] if resolved else None,
        "release_history": [
            {
                "tag": entry["tag_name"],
                "version": entry["tag_name"].removeprefix("v"),
                "source_commit": tag_map.get(entry["tag_name"]) if not malformed else None,
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


def markdown_tables(content: str) -> list[list[list[str]]]:
    """Return pipe tables as rows of stripped cells, without separator rows."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in [*content.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    return tables


def code_cell(cell: str) -> str | None:
    match = re.fullmatch(r"`([^`]+)`", cell)
    return match.group(1) if match else None


def extract_models(content: str) -> list[dict[str, str]]:
    """Extract only the explicit model/alias tables from the models page.

    Supports one model per row (``| Jev 1.13 | `jev-1.13.0` |``), one model per
    column with a ``Model ID`` row, and ``| `alias` | `target` |`` alias rows.
    """
    models: dict[tuple[str, str], dict[str, str]] = {}
    for table in markdown_tables(content):
        header = table[0]
        for row in table:
            if len(row) < 2:
                continue
            if row[0].lower() == "model id":
                # Every column must yield an identity; skipping one would promote a
                # parsing gap as a model removal.
                labels, cells = header[1:], row[1:]
                ids = [code_cell(cell) for cell in cells]
                if len(labels) != len(cells) or not all(labels) or not all(ids):
                    raise RuntimeError("unrecognized models table; model identities not promoted")
                for label, model_id in zip(labels, ids, strict=True):
                    models[("versioned", model_id)] = {
                        "kind": "versioned",
                        "id": model_id,
                        "label": label,
                    }
            elif row[0].lower().startswith("jev ") and code_cell(row[1]):
                model_id = code_cell(row[1])
                models[("versioned", model_id)] = {
                    "kind": "versioned",
                    "id": model_id,
                    "label": row[0],
                }
            elif code_cell(row[0]) and code_cell(row[1]):
                alias = code_cell(row[0])
                models[("alias", alias)] = {
                    "kind": "alias",
                    "id": alias,
                    "target": code_cell(row[1]),
                }
    return [models[key] for key in sorted(models)]


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
    published = registry.get("releases") if isinstance(registry, dict) else None
    published = published if isinstance(published, dict) else {}
    info = registry.get("info") if isinstance(registry, dict) else None
    package_name = info.get("name") if isinstance(info, dict) else None
    if not nonempty_string(package_name):
        published = {}
    for release in releases:
        version = release["version"]
        live = next((n for n in live_notes if n["version"] == version), None)
        if not live or not release.get("source_commit") or version not in published:
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
            sid = f"github:{repo}:{release['tag']}:{path}"
            artifact = artifacts.get(sid)
            if artifact is None or artifact.url != url:
                try:
                    response = client.get(url)
                except FetchError as exc:
                    if exc.status != 404:
                        raise
                    break
                artifact = Artifact(
                    sid,
                    "github-source",
                    url,
                    response.body,
                    upstream_commit=commit,
                    source_tag=release["tag"],
                )
                artifacts[sid] = artifact
            try:
                text = artifact.content.decode()
            except UnicodeError:
                break
            evidence.append({"source_id": sid, "url": url, "sha256": artifact.sha256})
            if path == "pyproject.toml":
                if parse_simple_toml_value(text, "version") != version or not package_names_match(
                    parse_simple_toml_value(text, "name"), package_name, "python"
                ):
                    break
            else:
                notes = next(
                    (n for n in parse_release_notes(text) if n["version"] == version),
                    None,
                )
        else:
            if not notes:
                continue
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
    branch = repo_meta.get("default_branch", "main")
    head_path = f"/repos/{repo}/commits/{branch}"
    head, head_response = github_json(client, head_path)
    artifacts[f"github:{repo}:head"] = Artifact(
        f"github:{repo}:head",
        "github-api",
        f"{GITHUB_API}{head_path}",
        evidence_bytes(stable_head_evidence(head)),
    )
    if package_kind == "python":
        registry_url = f"https://pypi.org/pypi/{package}/json"
        registry_response = client.get(registry_url)
        registry = registry_json(registry_response, package_kind, package)
        public_surface = ["TypeSafeClient", "AsyncTypeSafeClient", "Choice", "Noul", "Score"]
        registry_id = f"registry:pypi:{package}"
    else:
        registry_url = f"https://registry.npmjs.org/{package.replace('/', '%2f')}"
        registry_response = client.get(registry_url)
        registry = registry_json(registry_response, package_kind, package)
        public_surface = ["TypeSafeClient", "choice", "noul", "score"]
        registry_id = f"registry:npm:{package}"
    if package_kind == "python":
        version = registry["info"]["version"]
        registry_evidence = {
            "name": registry["info"]["name"],
            "version": version,
            "published_versions": sorted(registry["releases"]),
        }
    else:
        version = registry["dist-tags"]["latest"]
        registry_evidence = {"name": registry["name"], "latest": version}
    artifacts[registry_id] = Artifact(
        registry_id, "package-registry", registry_url, evidence_bytes(registry_evidence)
    )
    release_info = release_provenance(version, tags, releases)
    release_history = release_info["release_history"]
    runtime = "unknown"
    provenance = [
        {
            "source_id": registry_id,
            "url": registry_url,
            "sha256": artifacts[registry_id].sha256,
        }
    ]
    commit = release_info["source_commit"]
    if commit:
        raw_url = f"https://raw.githubusercontent.com/{repo}/{commit}/{config_path}"
        try:
            config_response = client.get(raw_url)
        except FetchError as exc:
            if exc.status != 404:
                raise
            release_info["discrepancies"].append("release_config_missing")
        else:
            config_artifact = Artifact(
                f"github:{repo}:{release_info['source_tag']}:{config_path}",
                "github-source",
                raw_url,
                config_response.body,
                upstream_commit=commit,
                source_tag=release_info["source_tag"],
            )
            artifacts[config_artifact.source_id] = config_artifact
            try:
                if package_kind == "python":
                    text = config_response.body.decode()
                    config_version = parse_simple_toml_value(text, "version")
                    config_name = parse_simple_toml_value(text, "name")
                    config_runtime = parse_simple_toml_value(text, "requires-python")
                else:
                    config = metadata_json(config_response, "SDK configuration")
                    if not isinstance(config, dict) or not isinstance(
                        config.get("engines", {}), dict
                    ):
                        raise schema_error(
                            config_response, "SDK configuration", "an object with an engines map"
                        )
                    config_version = config.get("version")
                    config_name = config.get("name")
                    config_runtime = config.get("engines", {}).get("node", "unknown")
                    if not isinstance(config_runtime, str):
                        raise schema_error(
                            config_response, "SDK configuration", "engines.node as a string"
                        )
            except (UnicodeError, json.JSONDecodeError):
                config_version = None
                config_name = None
                config_runtime = None
            if config_version is None:
                release_info["discrepancies"].append("release_config_malformed")
            elif config_version != version:
                release_info["discrepancies"].append("release_config_version_mismatch")
            elif not nonempty_string(config_name):
                release_info["discrepancies"].append("release_config_name_missing")
            elif not package_names_match(config_name, package, package_kind):
                release_info["discrepancies"].append("release_config_name_mismatch")
            else:
                runtime = config_runtime if isinstance(config_runtime, str) else "unknown"
                provenance.append(
                    {
                        "source_id": config_artifact.source_id,
                        "url": config_artifact.url,
                        "sha256": config_artifact.sha256,
                    }
                )
        if any(item.startswith("release_config_") for item in release_info["discrepancies"]):
            release_info["source_tag"] = None
            release_info["source_commit"] = None
    notes = parse_release_notes(changelog_content or "")
    for item in release_history:
        note = next((note for note in notes if note["version"] == item["version"]), None)
        if note:
            item["documented_changes"] = note["notes"]
            item["documented_changes_source_class"] = "mutable_documentation"
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
        "main_commit": head.get("sha") if isinstance(head, dict) else None,
        "runtime": {package_kind: runtime},
        "release_evidence_policy": "tag-verified-v1",
        "public_surface": {
            "source_class": "curated",
            "scope": "Manually curated SDK overview; not verified for this release.",
            "entry_points": public_surface,
            "primitives": ["Choice", "Noul", "Score"],
        },
        "release_history": sorted(
            release_history,
            key=lambda item: (item.get("published_at") or "", item.get("tag") or ""),
        ),
        "available_tags": release_info["available_tags"],
        "provenance": provenance,
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
        "discrepancies": sorted(set(release_info["discrepancies"])),
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
        evidence, evidence_reviews = rule_evidence(rule, contents, artifacts)
        prior = old.get(rule["id"])
        today = iso_day(utc_now())
        same_evidence = (
            prior and prior.get("sources") == evidence and prior.get("summary") == rule["summary"]
        )
        first_seen = prior.get("first_seen") if prior else None
        first_seen_basis = (
            prior.get("first_seen_basis", "observed" if first_seen else "unknown_legacy")
            if prior
            else "not_yet_supported"
        )
        if evidence and first_seen_basis == "not_yet_supported":
            first_seen = today
            first_seen_basis = "observed"
        last_verified = (
            prior.get("last_verified")
            if prior and prior.get("summary") == rule["summary"]
            else None
        )
        legacy_verification = (previous or {}).get("verification_semantics") != "evidence_revision"
        if legacy_verification:
            # Old unknown rules were assigned dates even without evidence. Reverify
            # supported facts now; do not promote those dates to verified history.
            last_verified = None
        if evidence and (not same_evidence or legacy_verification):
            last_verified = today
        item = {
            "id": rule["id"],
            "category": rule["category"],
            "status": "recommended" if evidence else "unknown",
            "summary": rule["summary"],
            # A legacy null is genuinely unknown; do not fabricate its first date.
            "first_seen": first_seen,
            "first_seen_basis": first_seen_basis,
            "last_verified": last_verified,
            "sources": evidence,
            "evidence_reviews": evidence_reviews,
            "interpretation": "Repository interpretation supported by literal claims and manually reviewed complete source contexts; not an upstream quote.",
        }
        item["last_changed"] = (
            prior.get("last_changed")
            if prior and practice_projection(prior) == practice_projection(item)
            else today
        )
        historical: list[dict[str, Any]] = []
        for source in [
            *(prior or {}).get("historical_sources", []),
            *(prior or {}).get("sources", []),
        ]:
            if source not in evidence and source not in historical:
                historical.append(source)
        if historical:
            item["historical_sources"] = historical
        items.append(item)
    state = {
        "schema_version": SCHEMA,
        "verification_semantics": "evidence_revision",
        "evidence_policy": PRACTICE_EVIDENCE_POLICY,
        "source_observations": {
            source_id: artifact.sha256
            for source_id in sorted({sid for rule in PRACTICE_RULES for sid in rule["sources"]})
            if (artifact := artifacts.get(source_id)) is not None
            and contents.get(source_id) == artifact.content.decode()
        },
        "status_vocabulary": [
            "recommended",
            "discouraged",
            "anti_pattern",
            "deprecated",
            "unknown",
        ],
        "items": items,
    }
    return observed_state(state, previous)


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
        extract_api(contents, artifacts, previous_state.get("api")),
        previous_state.get("api"),
    )
    models_content = contents.get("docs:models")
    model_items = extract_models(models_content or "")
    if models_content is not None and not any(item["kind"] == "versioned" for item in model_items):
        # An unrecognized layout must not silently drop model identities.
        raise RuntimeError("unrecognized models table; model identities not promoted")
    models = observed_state(
        {
            "schema_version": SCHEMA,
            "models": model_items,
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


# Every item in these layers comes from a fixed rule table in this module.
CODE_DEFINED_LAYERS = frozenset({"practices", "primitives"})


def _practice_event_sources(before, after):
    references = {}
    for role, item in (("historical", before or {}), ("current", after or {})):
        for source in [*item.get("evidence_reviews", []), *item.get("sources", [])]:
            if not all(source.get(key) for key in ("source_id", "sha256", "url")):
                continue
            key = role, source["source_id"], source["sha256"], source["url"]
            reference = references.setdefault(
                key,
                {name: source[name] for name in ("source_id", "sha256", "url")} | {"role": role},
            )
            for name in ("context_sha256", "excerpt"):
                if source.get(name) is not None:
                    reference[name] = source[name]
            if source.get("verdict"):
                reference["review_verdict"] = source["verdict"]
    return [references[key] for key in sorted(references)]


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
    old_practices = old_state.get("practices", {})
    prior_source_hashes = {
        source["source_id"]: source.get("sha256")
        for item in old_practices.get("items", [])
        for source in item.get("sources", [])
        if source.get("source_id")
    }
    prior_source_hashes.update(old_practices.get("source_observations", {}))
    prior_source_hashes.update(
        {
            source["source_id"]: source.get("sha256")
            for source in old_state.get("sources", {}).get("artifacts", [])
            if source.get("source_id")
        }
    )
    practice_rule_sources = {rule["id"]: rule["sources"] for rule in PRACTICE_RULES}
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
            practice_review_changed = (
                name == "practices"
                and entity_key in old_items
                and entity_key in new_items
                and old_state.get(name, {}).get("evidence_policy") == PRACTICE_EVIDENCE_POLICY
                and new_state.get(name, {}).get("evidence_policy") == PRACTICE_EVIDENCE_POLICY
                and old_items[entity_key].get("evidence_reviews")
                and old_items[entity_key].get("summary") == new_items[entity_key].get("summary")
                and old_items[entity_key].get("evidence_reviews")
                != new_items[entity_key].get("evidence_reviews")
                and practice_rule_sources.get(entity_key)
                and all(
                    old_state[name].get("source_observations", {}).get(sid)
                    == new_state[name].get("source_observations", {}).get(sid)
                    for sid in practice_rule_sources[entity_key]
                )
            )
            if practice_review_changed:
                before_value = {
                    "practice": before_value,
                    "context_reviews": sorted(
                        old_items[entity_key]["evidence_reviews"], key=lambda row: row["source_id"]
                    ),
                }
                after_value = {
                    "practice": after_value,
                    "context_reviews": sorted(
                        new_items[entity_key]["evidence_reviews"], key=lambda row: row["source_id"]
                    ),
                }
            before = sha256_json(before_value) if entity_key in old_items else None
            after = sha256_json(after_value) if entity_key in new_items else None
            sdk_policy_baseline = (
                name.startswith("sdk-")
                and entity_key in old_items
                and not old_items[entity_key].get("release_evidence_policy")
                and new_items.get(entity_key, {}).get("release_evidence_policy")
                == "tag-verified-v1"
                and all(
                    old_items[entity_key].get(field) == new_items.get(entity_key, {}).get(field)
                    for field in ("version", "source_tag", "source_commit")
                )
                and old_items[entity_key].get("discrepancies", [])
                == new_items.get(entity_key, {}).get("discrepancies", [])
                and before_value["release_history"] == after_value["release_history"]
            )
            if before == after and not sdk_policy_baseline:
                continue
            change = "added" if before is None else "removed" if after is None else "modified"
            tracking_note = None
            if sdk_policy_baseline:
                change = "baseline"
                tracking_note = (
                    f"Established {name} tag-verified release evidence tracking; "
                    "local correction of legacy SDK extraction/provenance, "
                    "not an upstream release change."
                )
            if name in CODE_DEFINED_LAYERS and change != "modified":
                # These entities exist because of an extraction rule in this repository,
                # so appearing or disappearing is a tracking change, not upstream history.
                change = "baseline" if change == "added" else "untracked"
                tracking_note = (
                    f"Started tracking {category} {entity_key} with a new extraction rule; "
                    "not an upstream addition."
                    if change == "baseline"
                    else f"Stopped tracking {category} {entity_key}; its extraction rule was "
                    "removed. This is not upstream removal or deprecation."
                )
            if (
                name == "model-limitations"
                and entity_key in old_items
                and entity_key in new_items
                and (
                    "summary_kind" not in old_items[entity_key]
                    and "summary_kind" in new_items[entity_key]
                    or "complete_section_sha256" not in old_items[entity_key]
                    and "complete_section_sha256" in new_items[entity_key]
                )
            ):
                evidence_fields = ("id", "model", "section_text_sha256", "status")
                same_evidence = all(
                    old_items[entity_key].get(field) == new_items[entity_key].get(field)
                    for field in evidence_fields
                )
                old_complete = old_items[entity_key].get("complete_section_sha256")
                new_complete = new_items[entity_key].get("complete_section_sha256")
                if old_complete is not None:
                    same_evidence = same_evidence and old_complete == new_complete
                elif new_complete is not None:
                    old_hashes = [
                        source.get("sha256")
                        for source in old_items[entity_key].get("provenance", [])
                    ]
                    new_hashes = [
                        source.get("sha256")
                        for source in new_items[entity_key].get("provenance", [])
                    ]
                    same_evidence = same_evidence and bool(old_hashes) and old_hashes == new_hashes
                change = "baseline" if same_evidence else "modified"
                tracking_note = (
                    f"Updated local interpretation and quotation policy for {entity_key}; "
                    + (
                        "established a tracking baseline, not an upstream limitation change."
                        if same_evidence
                        else "observed limitation evidence or status also changed during "
                        "the local quotation-policy migration."
                    )
                )
            elif (
                name == "practices"
                and entity_key in old_items
                and entity_key in new_items
                and not old_state.get(name, {}).get("evidence_policy")
                and new_state.get(name, {}).get("evidence_policy") == PRACTICE_EVIDENCE_POLICY
                and old_items[entity_key].get("sources")
                and practice_rule_sources.get(entity_key)
                and all(
                    prior_source_hashes.get(source_id)
                    and new_state[name].get("source_observations", {}).get(source_id)
                    == prior_source_hashes[source_id]
                    for source_id in practice_rule_sources[entity_key]
                )
            ):
                change = "baseline"
                tracking_note = (
                    f"Reassessed practice {entity_key} evidence status "
                    f"{old_items[entity_key].get('status', 'unknown')} → "
                    f"{new_items[entity_key].get('status', 'unknown')} under the local "
                    f"{PRACTICE_EVIDENCE_POLICY} review policy. This is a tracking correction, "
                    "not an upstream guidance change, removal or deprecation."
                )
            elif practice_review_changed:
                change = "verification_changed"
                tracking_note = (
                    f"Recorded local context review for practice {entity_key}: "
                    f"{old_items[entity_key].get('status')} → {new_items[entity_key].get('status')}. "
                    "Retained source observations did not change; this does not date an "
                    "upstream guidance introduction, removal or deprecation."
                )
            item = new_items.get(entity_key, old_items.get(entity_key, {}))
            verification_sources = None
            old_api = old_items.get(entity_key) or {}
            if name == "api" and "properties" in old_api and "properties" in item:
                old_properties = old_api["properties"]
                new_properties = item["properties"]
                fields = set(old_properties) | set(new_properties)
                status_changed = any(
                    old_properties.get(field, {}).get("status")
                    != new_properties.get(field, {}).get("status")
                    for field in fields
                )
                contract_changed = False
                for field in fields:
                    prior_fact = old_properties.get(field, {})
                    if prior_fact.get("status") != "verified":
                        prior_fact = prior_fact.get("last_known", {})
                    current_fact = new_properties.get(field, {})
                    if (
                        prior_fact.get("status") == "verified"
                        and current_fact.get("status") == "verified"
                        and prior_fact["value"] != current_fact["value"]
                    ):
                        contract_changed = True
                if status_changed and not contract_changed:
                    change = "verification_changed"
                    tracking_note = (
                        "API reference verification status changed; loss or recovery of evidence "
                        "does not establish an upstream contract change."
                    )
                if contract_changed:
                    tracking_note = (
                        "Verified HTTP reference facts differ from the last supported observation; "
                        "the exact upstream change time is unknown."
                    )
                if status_changed or contract_changed:
                    verification_sources = [
                        {**source, "role": "current"} for source in item.get("provenance", [])
                    ]
                    historical = old_api.get("provenance", []) + old_api.get("missing_sources", [])
                    for prop in old_properties.values():
                        historical += prop.get("evidence", []) + prop.get("last_known", {}).get(
                            "evidence", []
                        )
                    for source in historical:
                        retained = {
                            key: source[key]
                            for key in ("source_id", "url", "sha256")
                            if key in source
                        }
                        retained["role"] = "historical"
                        if retained not in verification_sources:
                            verification_sources.append(retained)
            if (
                name == "api"
                and "properties" in item
                and "properties" not in (old_items.get(entity_key) or {})
            ):
                change = "baseline"
                old_api = old_items.get(entity_key) or {}
                old_hashes = {
                    source.get("sha256")
                    for source in old_api.get("provenance", [])
                    if source.get("source_id") == "docs:api" and source.get("sha256")
                }
                new_hashes = {
                    source.get("sha256")
                    for source in item.get("provenance", [])
                    if source.get("source_id") == "docs:api" and source.get("sha256")
                }
                has_verified_evidence = bool(item.get("provenance")) and any(
                    prop.get("status") == "verified" for prop in item["properties"].values()
                )
                if not has_verified_evidence:
                    change = "verification_changed"
                    tracking_note = (
                        "Classified previously curated HTTP assumptions as unknown; "
                        "no current API property has verified reference evidence. "
                        "No upstream behavior is inferred from missing evidence."
                    )
                    verification_sources = [
                        {**source, "role": "current_observation"}
                        for source in item.get("provenance", [])
                    ] + [
                        {**source, "role": "historical"} for source in old_api.get("provenance", [])
                    ]
                elif len(old_hashes) == len(new_hashes) == 1 and old_hashes == new_hashes:
                    tracking_note = (
                        "Established claim-level HTTP reference verification; corrected repository "
                        "assumptions, not an upstream API introduction or contract change."
                    )
                else:
                    source_note = (
                        "upstream source changed; "
                        if old_hashes and new_hashes and old_hashes != new_hashes
                        else ""
                    )
                    tracking_note = (
                        "Established first verified snapshot of the HTTP reference; "
                        + source_note
                        + "no historical API behavior inferred from earlier curated assumptions."
                    )
            status_note = None
            if name == "practices" and change == "modified":
                old_status = old_items[entity_key].get("status", "unknown")
                new_status = new_items[entity_key].get("status", "unknown")
                if old_status != new_status:
                    status_note = (
                        f"Practice {entity_key} evidence status changed: "
                        f"{old_status} → {new_status}. "
                    )
                    if new_status == "unknown":
                        status_note += "Current support is insufficient; absence is not removal or deprecation."
                    elif new_status == "recommended":
                        status_note += "Current canonical evidence supports the recommendation."
            events.append(
                {
                    "schema_version": SCHEMA,
                    "id": event_id(f"{name}:{entity_key}", change, before, after),
                    "observed_at": observed_at,
                    "category": category,
                    "entity": entity_key,
                    "change": change,
                    "summary": status_note
                    or item.get("summary")
                    or (
                        f"{item.get('package')} {old_items.get(entity_key, {}).get('version', 'unknown')} → {item.get('version')}; release guidance and capability evidence updated."
                        if name.startswith("sdk-")
                        else models_summary(item)
                        if name == "models"
                        else f"{name} {entity_key} changed"
                    ),
                    "before_sha256": before,
                    "after_sha256": after,
                    "sources": verification_sources
                    if verification_sources is not None
                    else _practice_event_sources(
                        old_items.get(entity_key), new_items.get(entity_key)
                    )
                    if name == "practices"
                    else item.get("sources")
                    or item.get("provenance")
                    or ([item["source"]] if item.get("source") else []),
                }
            )
            if tracking_note:
                events[-1]["summary"] = tracking_note
    return events


def models_summary(state: dict[str, Any]) -> str:
    entries = state.get("models", [])
    versioned = [entry["id"] for entry in entries if entry.get("kind") == "versioned"]
    aliases = [
        f"{entry['id']} → {entry.get('target')}"
        for entry in entries
        if entry.get("kind") == "alias"
    ]
    return (
        "Documented model identities: "
        + (", ".join(versioned) or "none")
        + "; aliases: "
        + (", ".join(aliases) or "none")
        + "."
    )


def practice_projection(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {key: item.get(key) for key in ("id", "category", "status", "summary")}


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
                "required_fields",
                "model_required",
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
        if isinstance(projection["public_surface"], dict):
            projection["public_surface"] = {
                key: value
                for key, value in projection["public_surface"].items()
                if key not in {"source_class", "scope"}
            }
        projection["release_history"] = [
            {
                key: value
                for key, value in release.items()
                if key
                not in {
                    "source_commit",
                    "documented_changes_provenance",
                    "documented_changes_source_class",
                }
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


def chain_events(
    candidates: Iterable[dict[str, Any]], historical: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Identify transitions by their predecessor, not their observation clock."""
    ordered = sorted(historical, key=lambda event: event.get("observed_at") or "")
    by_id = {event["id"]: event for event in ordered}
    children: dict[str, list[dict[str, Any]]] = {}
    for event in ordered:
        previous = event.get("previous_event_id")
        if previous in by_id:
            children.setdefault(previous, []).append(event)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    visited: set[str] = set()
    for root in ordered:
        if root.get("previous_event_id") in by_id:
            continue
        pending = [root]
        while pending:
            event = pending.pop()
            if event["id"] in visited:
                raise RuntimeError("event history contains a cycle")
            visited.add(event["id"])
            latest[(event["category"], event["entity"])] = event
            pending.extend(reversed(children.get(event["id"], [])))
    if len(visited) != len(by_id):
        raise RuntimeError("event history contains a cycle")

    result = []
    transition_keys = ("change", "before_sha256", "after_sha256")
    for candidate in candidates:
        key = (candidate["category"], candidate["entity"])
        previous = latest.get(key)
        event = dict(candidate)
        if previous and all(
            candidate.get(field) == previous.get(field) for field in transition_keys
        ):
            # Events may already be durable while their corresponding state is not.
            event["id"] = previous["id"]
            event["previous_event_id"] = previous.get("previous_event_id")
        else:
            event["previous_event_id"] = previous["id"] if previous else None
            if previous:
                event["id"] = hashlib.sha256(
                    f"jev-docs-event-chain-v1|{candidate['id']}|{previous['id']}".encode()
                ).hexdigest()[:24]
        latest[key] = event
        result.append(event)
    return result


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


def load_history_events(events_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(events_dir.glob("*.json")):
        data = read_json(path, {})
        records.extend(
            event for event in data.get("events", []) if isinstance(event, dict) and event.get("id")
        )
    return records


def render_best_practices(practices: dict[str, Any]) -> str:
    latest = max(
        (item["last_verified"] for item in practices["items"] if item.get("last_verified")),
        default="unknown",
    )
    reviewed = practices.get("evidence_policy") == PRACTICE_EVIDENCE_POLICY
    lines = [
        "# Jev Engineering Best Practices",
        "",
        f"Evidence revisions last verified: {latest}"
        if practices.get("verification_semantics") == "evidence_revision"
        else f"Last verified: {latest}",
        "",
        (
            "Generated from [`state/practices.json`](state/practices.json). Recommendations are repository interpretations, not upstream quotes. `recommended` requires both complete literal passage bundles and explicit manual review of the complete source context. Changed or unreviewed contexts remain `unknown` until reviewed; absence never implies deprecation."
            if reviewed
            else "Generated from [`state/practices.json`](state/practices.json). `recommended` means current canonical evidence matched a transparent extraction rule; `unknown` means this snapshot does not provide enough evidence."
        ),
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
            summary = item["summary"]
            if reviewed and item["status"] == "unknown":
                summary = f"Unverified candidate: {summary}"
            lines.extend([f"### {item['id']} — `{item['status']}`", "", summary, ""])
            if item["sources"]:
                lines.append("Evidence:")
                for evidence in item["sources"]:
                    lines.append(
                        f"- [{evidence['source_id']}]({evidence['url']}) — {evidence['excerpt']}"
                    )
            else:
                lines.append(
                    "Evidence: no current verified recommendation in this observation."
                    if reviewed
                    else "Evidence: no matching canonical source in this observation."
                )
            if reviewed and item["status"] == "unknown":
                pending = [
                    review["source_id"]
                    for review in item.get("evidence_reviews", [])
                    if review["verdict"] in {"unreviewed", "unverified_passages"}
                ]
                rejected = [
                    review["source_id"]
                    for review in item.get("evidence_reviews", [])
                    if review["verdict"] == "rejected"
                ]
                if pending:
                    lines.append("Context review required: " + ", ".join(pending) + ".")
                if rejected:
                    lines.append("Reviewed conflicting context: " + ", ".join(rejected) + ".")
            if item.get("historical_sources"):
                lines.append("Historical evidence (not current support):")
                for evidence in item["historical_sources"]:
                    lines.append(
                        f"- [{evidence['source_id']}]({evidence['url']}) — {evidence['excerpt']}"
                    )
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
    if (
        not isinstance(coverage, dict)
        or type(coverage.get("schema_version")) is not int
        or coverage["schema_version"] not in {1, 2}
    ):
        raise RuntimeError("unsupported source coverage schema: expected 1 or 2")
    pages = coverage["pages"]
    if not pages:
        raise RuntimeError("source coverage contains no documentation pages")
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


def _validate_event_graph(events: dict[str, dict[str, Any]]) -> None:
    successors: dict[str, str] = {}
    for ident, event in events.items():
        previous_id = event.get("previous_event_id")
        if previous_id is None:
            # Legacy records need not form a single chain for each entity.
            continue
        if not isinstance(previous_id, str) or previous_id not in events:
            raise RuntimeError(f"event {ident} has missing predecessor {previous_id}")
        previous = events[previous_id]
        if (event.get("category"), event.get("entity")) != (
            previous.get("category"),
            previous.get("entity"),
        ):
            raise RuntimeError(f"event {ident} predecessor crosses category or entity")
        if previous_id in successors:
            raise RuntimeError(f"event {previous_id} has multiple successors")
        successors[previous_id] = ident

    completed: set[str] = set()
    for ident in events:
        chain: set[str] = set()
        current: str | None = ident
        while current is not None and current not in completed:
            if current in chain:
                raise RuntimeError(f"event {current} predecessor chain contains a cycle")
            chain.add(current)
            current = events[current].get("previous_event_id")
        completed.update(chain)


def _source_records(value: Any, filename: str, source: str | None = None) -> dict[str, Any]:
    expected = (
        {"source": source}
        if source is not None
        else {"scope": SOURCE_SCOPE, "documentation_license_note": DOCUMENTATION_LICENSE_NOTE}
    )
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA
        or not isinstance(value.get("artifacts"), list)
        or any(value.get(key) != required for key, required in expected.items())
    ):
        raise RuntimeError(f"invalid source manifest envelope: {filename}")
    records: dict[str, Any] = {}
    for item in value["artifacts"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source_id"), str)
            or not item["source_id"]
        ):
            raise RuntimeError(f"invalid source record in {filename}")
        sid = item["source_id"]
        if sid in records:
            raise RuntimeError(f"duplicate source ID in {filename}: {sid}")
        records[sid] = item
    return records


def _trusted_source_location(record):
    sid = record["source_id"].removeprefix("candidate:")
    kind = record.get("source_type")
    url = record.get("fetch_url")
    commit = record.get("upstream_commit")
    source_tag = record.get("source_tag")
    if source_tag is not None and (not isinstance(source_tag, str) or not source_tag):
        return False
    if not isinstance(url, str):
        return False
    if kind in DOCS_SOURCE_TYPES:
        return (
            commit is None
            and source_tag is None
            and https_origin(url) == https_origin(DOCS_ROOT)
            and record.get("canonical_url") == docs_canonical_url(url)
        )
    if record.get("canonical_url") != url:
        return False
    registries = {
        "registry:pypi:typesafe-sdk": "https://pypi.org/pypi/typesafe-sdk/json",
        "registry:npm:@typesafe-ai/sdk": "https://registry.npmjs.org/@typesafe-ai%2fsdk",
    }
    if sid in registries:
        return (
            kind == "package-registry"
            and url == registries[sid]
            and commit is None
            and source_tag is None
        )
    if sid == "skill:typesafe-ai":
        return (
            kind == "github-source"
            and source_tag is None
            and isinstance(commit, str)
            and re.fullmatch(r"[0-9a-f]{40}", commit)
            and url
            == f"https://raw.githubusercontent.com/typesafe-ai/skills/{commit}/skills/typesafe-ai/SKILL.md"
        )
    match = re.fullmatch(r"github:(typesafe-ai/[^:]+):(.+)", sid)
    if not match:
        return False
    repo, identity = match.groups()
    evaluation = repo == "typesafe-ai/system-one-adapter-python"
    if repo not in {
        "typesafe-ai/skills",
        "typesafe-ai/typesafe-sdk-python",
        "typesafe-ai/typesafe-sdk-js",
        "typesafe-ai/system-one-adapter-python",
    }:
        return False
    api_identity = identity in {"repository", "head", "tags", "releases", "commit"}
    if api_identity and (commit is not None or source_tag is not None):
        return False
    if not api_identity:
        expected_kind = "official_evaluation_tool" if evaluation else "github-source"
        path = identity
        if ":" in path:
            tag, path = path.split(":", 1)
            if tag != source_tag:
                return False
        return (
            kind == expected_kind
            and isinstance(commit, str)
            and re.fullmatch(r"[0-9a-f]{40}", commit)
            and url == f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
        )
    if kind != ("official_evaluation_tool" if evaluation else "github-api"):
        return False
    base = f"{GITHUB_API}/repos/{repo}"
    if identity == "head":
        prefix = base + "/commits/"
        return url.startswith(prefix) and bool(url[len(prefix) :]) and not re.search(r"[?#]", url)
    if identity == "commit" and repo == "typesafe-ai/skills":
        return url == base + "/commits?path=skills/typesafe-ai/SKILL.md&per_page=1"
    expected = {
        "repository": base,
        "tags": base + "/tags?per_page=100",
        "releases": base + "/releases?per_page=100",
    }
    return identity in expected and url == expected[identity]


def _retained_tag_bindings(state_files):
    catalogs = {}
    for kind, repo in (
        ("python", "typesafe-ai/typesafe-sdk-python"),
        ("javascript", "typesafe-ai/typesafe-sdk-js"),
        ("evaluation-tool", "typesafe-ai/system-one-adapter-python"),
    ):
        snapshot = state_files.get(
            "evaluation-tool.json" if kind == "evaluation-tool" else f"sdk-{kind}.json", {}
        )
        bindings = {}
        for release in [
            *snapshot.get("release_history", []),
            {"tag": snapshot.get("source_tag"), "source_commit": snapshot.get("source_commit")},
        ]:
            tag, commit = release.get("tag"), release.get("source_commit")
            if tag is None or commit is None:
                continue
            if (
                not isinstance(tag, str)
                or not isinstance(commit, str)
                or not re.fullmatch(r"[0-9a-f]{40}", commit)
            ):
                raise RuntimeError(f"invalid retained release binding: {repo}")
            if tag in bindings and bindings[tag] != commit:
                raise RuntimeError(f"conflicting retained release binding: {repo}")
            bindings[tag] = commit
        catalogs[(repo, False)] = bindings
        if kind != "evaluation-tool":
            # Legacy held candidates were explicitly unresolved, not verified
            # release claims. A new collection persists their observed bindings.
            candidate = state_files.get("freshness.json", {}).get(kind, {})
            candidate_bindings = candidate.get("candidate_tag_bindings")
            if candidate_bindings is not None and (
                not isinstance(candidate_bindings, dict)
                or any(
                    not isinstance(tag, str)
                    or not tag
                    or not isinstance(commit, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", commit)
                    for tag, commit in candidate_bindings.items()
                )
            ):
                raise RuntimeError(f"invalid candidate release binding: {repo}")
            catalogs[(repo, True)] = candidate_bindings if candidate.get("state_retained") else {}
    return catalogs


def _validate_source_integrity(root, coverage, sources, state_files):
    tag_catalogs = _retained_tag_bindings(state_files)
    for sid, record in sources.items():
        if not _trusted_source_location(record):
            raise RuntimeError(f"source identity URL mismatch: {sid}")
        if record.get("source_tag") is not None:
            candidate = sid.startswith("candidate:")
            identity = sid.removeprefix("candidate:")
            repo = identity.split(":", 2)[1]
            catalog = tag_catalogs.get((repo, candidate), {})
            if catalog is None and candidate:
                continue  # Legacy candidates remain unverified until recollected.
            if (
                not isinstance(catalog, dict)
                or catalog.get(record["source_tag"]) != record["upstream_commit"]
            ):
                raise RuntimeError(f"tagged source disagrees with retained release binding: {sid}")
    skill = state_files.get("skill.json", {})
    citation = skill.get("source") if isinstance(skill, dict) else None
    raw_skill = sources.get("skill:typesafe-ai", {})
    skill_head = sources.get("github:typesafe-ai/skills:commit", {})
    if (
        not isinstance(citation, dict)
        or citation.get("source_id") != "skill:typesafe-ai"
        or citation.get("sha256") != raw_skill.get("sha256")
        or citation.get("url") != raw_skill.get("fetch_url")
        or skill.get("commit") != raw_skill.get("upstream_commit")
        or skill_head.get("sha256") != sha256_bytes(evidence_bytes({"sha": skill.get("commit")}))
    ):
        raise RuntimeError(
            "skill provenance disagrees with retained snapshot and commit observation"
        )
    for kind, repo in (
        ("python", "typesafe-ai/typesafe-sdk-python"),
        ("javascript", "typesafe-ai/typesafe-sdk-js"),
    ):
        sdk = state_files[f"sdk-{kind}.json"]
        main = sdk.get("main_commit")
        head = sources.get(f"github:{repo}:head", {})
        if (
            not isinstance(main, str)
            or not re.fullmatch(r"[0-9a-f]{40}", main)
            or head.get("sha256") != sha256_bytes(evidence_bytes({"sha": main}))
        ):
            raise RuntimeError(f"SDK main provenance disagrees with retained snapshot: {kind}")
    for name in ("sdk-python", "sdk-javascript", "evaluation-tool"):
        snapshot = state_files.get(f"{name}.json", {})
        for citation in snapshot.get("provenance", []):
            record = sources.get(citation.get("source_id"), {})
            if (
                not record
                or citation.get("sha256") != record.get("sha256")
                or citation.get("url") != record.get("fetch_url")
            ):
                raise RuntimeError(f"snapshot citation disagrees with retained source: {name}")
    families = {}
    manifests_present = False
    for filename, source in (
        ("sources/docs.typesafe.ai/manifest.json", DOCS_ROOT),
        ("sources/github.typesafe-ai/manifest.json", GITHUB_SOURCE_ROOT),
    ):
        manifest = read_json(root / filename)
        if manifest is None:  # Coverage 1 trees may predate the complete layout.
            continue
        manifests_present = True
        records = _source_records(manifest, filename, source)
        if any(
            (record.get("source_type") in DOCS_SOURCE_TYPES) != (source == DOCS_ROOT)
            for record in records.values()
        ):
            raise RuntimeError(f"source record belongs to wrong family: {filename}")
        if families.keys() & records.keys():
            raise RuntimeError("duplicate source ID across family manifests")
        families.update(records)
    if coverage.get("schema_version") == 2 or manifests_present:
        # Phase 1 wrote family and aggregate observations separately. Their clocks
        # may differ by microseconds; compare semantic metadata without rewriting it.
        def semantic(record):
            return {key: value for key, value in record.items() if key != "observed_at"}

        if families.keys() != sources.keys() or any(
            semantic(record) != semantic(sources[sid]) for sid, record in families.items()
        ):
            raise RuntimeError("family manifests disagree with state/sources.json")
    discovery = coverage["discovery"]
    record = sources.get(discovery["source_id"])
    if (
        not record
        or record["source_type"] != "documentation-discovery"
        or record["sha256"] != discovery["sha256"]
        or record["fetch_url"] != discovery["url"]
        or https_origin(discovery["url"]) != https_origin(DOCS_ROOT)
        or record["canonical_url"] != docs_canonical_url(record["fetch_url"])
        or docs_source_id(discovery["url"]) != discovery["source_id"]
    ):
        raise RuntimeError("coverage discovery disagrees with current source artifact")
    for page in coverage["pages"]:
        record = sources.get(page["source_id"])
        if (
            not record
            or record["source_type"] != "documentation"
            or record["sha256"] != page["sha256"]
            # Coverage retains requested navigation; manifests record the resolved
            # same-origin destination, which may legitimately have a different path.
            or https_origin(record["fetch_url"]) != https_origin(DOCS_ROOT)
            or record["canonical_url"] != docs_canonical_url(record["fetch_url"])
            or https_origin(page["canonical_url"]) != https_origin(DOCS_ROOT)
            or docs_source_id(page["canonical_url"]) != page["source_id"]
        ):
            raise RuntimeError("coverage page disagrees with current source artifact")
    current_ids = {
        sid
        for sid, record in sources.items()
        if record["source_type"] == "documentation" and sid.startswith("docs:")
    }
    if current_ids != {page["source_id"] for page in coverage["pages"]}:
        raise RuntimeError("coverage pages disagree with current documentation sources")


def validate_tree(root: Path, strict: bool = False, *, history_root: Path | None = None) -> None:
    if (root / PENDING).exists() or (root / PENDING).is_symlink():
        raise RuntimeError("pending synchronization promotion; run sync to recover it")
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
    state_files = {path.name: read_json(path) for path in sorted((root / "state").glob("*.json"))}
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
    sources = _source_records(read_json(root / "state/sources.json"), "state/sources.json")
    for kind in ("python", "javascript"):
        sdk = read_json(root / f"state/sdk-{kind}.json", {})
        for release in sdk.get("release_history", []):
            for evidence in release.get("documented_changes_provenance", []):
                record = sources.get(evidence.get("source_id"), {})
                if not record or record["sha256"] != evidence.get("sha256"):
                    raise RuntimeError(
                        f"SDK changelog provenance does not match retained evidence: {kind}"
                    )
    freshness = read_json(root / "state/freshness.json", {})
    for kind in ("python", "javascript"):
        for evidence in freshness.get(kind, {}).get("candidate_provenance", []):
            record = sources.get(evidence.get("source_id"), {})
            if not record or evidence.get("sha256") != record["sha256"]:
                raise RuntimeError(
                    f"Candidate SDK provenance does not match retained evidence: {kind}"
                )
    validate_api(read_json(root / "state/api.json"), sources)
    for item in practices.get("items", []):
        if item["status"] != "recommended":
            continue
        if practices.get("evidence_policy") == PRACTICE_EVIDENCE_POLICY:
            rule = next((rule for rule in PRACTICE_RULES if rule["id"] == item["id"]), None)
            reviews = item.get("evidence_reviews", [])
            identities = [review.get("source_id") for review in reviews]
            if (
                rule is None
                or not item.get("sources")
                or any(item.get(key) != rule[key] for key in ("category", "summary"))
                or len(identities) != len(set(identities))
                or set(identities) != set(rule["sources"])
            ):
                raise RuntimeError(f"recommended practice lacks reviewed context: {item['id']}")
            for review in reviews:
                sid, verdict = review["source_id"], review.get("verdict")
                current = sources.get(sid)
                if verdict == "unavailable" and current is None:
                    continue
                context_hash = review.get("context_sha256")
                approved = (
                    REVIEWED_PRACTICE_CONTEXTS.get(sid, {}).get(context_hash, {})
                    if isinstance(context_hash, str)
                    else {}
                )
                if (
                    verdict not in {"supported", "non_conflicting"}
                    or approved.get("verdicts", {}).get(item["id"]) != verdict
                    or review.get("review_source")
                    != {"url": approved.get("source_url"), "sha256": approved.get("source_sha256")}
                    or not current
                    or review.get("sha256") != current["sha256"]
                    or review.get("url") != current["fetch_url"]
                ):
                    raise RuntimeError(f"recommended practice lacks authored review: {item['id']}")
        # Historical sources describe prior observations, not current support.
        for evidence in item.get("sources", []):
            current = sources.get(evidence["source_id"])
            if not current or evidence["sha256"] != current["sha256"]:
                raise RuntimeError(
                    f"practice provenance does not match retained artifact: {item['id']}"
                )
            if practices.get("evidence_policy") == PRACTICE_EVIDENCE_POLICY:
                reviews = item.get("evidence_reviews", [])
                if (
                    not reviews
                    or any(
                        review.get("verdict") not in {"supported", "non_conflicting", "unavailable"}
                        for review in reviews
                    )
                    or not any(
                        review.get("verdict") == "supported"
                        and review.get("source_id") == evidence["source_id"]
                        and review.get("sha256") == evidence["sha256"]
                        and review.get("url") == evidence.get("url") == current.get("fetch_url")
                        and review.get("context_sha256") == evidence.get("context_sha256")
                        and isinstance(evidence.get("context_sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", evidence["context_sha256"])
                        for review in reviews
                    )
                ):
                    raise RuntimeError(f"recommended practice lacks reviewed context: {item['id']}")
                expected_claims = {claim for _digest, claim in rule["claims"]}
                cited_claims = {
                    ref.get("excerpt")
                    for ref in item["sources"]
                    if ref.get("source_id") == evidence["source_id"]
                }
                if cited_claims != expected_claims:
                    raise RuntimeError(
                        f"recommended practice lacks literal claim bundle: {item['id']}"
                    )
    # Only recorded observations assert a current fetch; absent optional sources
    # and unknown practices' historical evidence need not have retained artifacts.
    for source_id, digest in practices.get("source_observations", {}).items():
        current = sources.get(source_id)
        if not current or digest != current["sha256"]:
            raise RuntimeError(
                f"practice observation does not match retained artifact: {source_id}"
            )
    if coverage.get("schema_version") == 2:
        required_v2 = [
            *(
                f"state/{name}.json"
                for name in (
                    "api",
                    "models",
                    "primitives",
                    "model-limitations",
                    "cookbooks",
                    "evaluation-tool",
                )
            ),
            "MODEL_LIMITATIONS.md",
            "sources/docs.typesafe.ai/manifest.json",
            "sources/github.typesafe-ai/manifest.json",
        ]
        missing = [path for path in required_v2 if not (root / path).exists()]
        if missing:
            raise RuntimeError(f"missing generated files: {', '.join(missing)}")
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
    event_paths: dict[str, Path] = {}
    report_paths: dict[str, Path] = {}
    for history in (history_root, root):
        if history is not None:
            event_paths.update({path.stem: path for path in (history / "events").glob("*.json")})
            report_paths.update({path.stem: path for path in (history / "changes").glob("*.md")})
    seen_event_ids: dict[str, str] = {}
    events_by_id: dict[str, dict[str, Any]] = {}
    for _, path in sorted(event_paths.items()):
        data = read_json(path)
        ids = [event.get("id") for event in data.get("events", [])]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"duplicate event ID in {path}")
        if not strict or path.name == "baseline.json":
            continue
        # Strict: history is append-safe across files and each day's report is the
        # deterministic rendering of that day's events.
        for ident in ids:
            if ident in seen_event_ids:
                raise RuntimeError(f"event {ident} recorded in {seen_event_ids[ident]} and {path}")
            seen_event_ids[ident] = str(path)
        events_by_id.update({event["id"]: event for event in data.get("events", [])})
        if data.get("date") != path.stem:
            raise RuntimeError(f"event file date disagrees with its name: {path}")
        report = report_paths.get(path.stem)
        if (
            report is None
            or not report.exists()
            or report.read_text() != render_daily_report(path.stem, data.get("events", []))
        ):
            raise RuntimeError(f"daily report disagrees with events: {report}")
    if strict:
        _validate_event_graph(events_by_id)
        event_days = set(event_paths) - {"baseline"}
        orphaned = sorted(path.name for day, path in report_paths.items() if day not in event_days)
        if orphaned:
            raise RuntimeError(f"daily reports without event files: {', '.join(orphaned)}")
    if "state/practices.json" not in (root / "BEST_PRACTICES.md").read_text():
        raise RuntimeError("best-practices guide is not linked to generated state")
    if strict and (root / "BEST_PRACTICES.md").read_text() != render_best_practices(practices):
        raise RuntimeError("best-practices rendering disagrees with state")
    _validate_source_integrity(root, coverage, sources, state_files)
    # Keep domain-specific provenance diagnostics ahead of this final checksum guard.
    for filename, value in state_files.items():
        if filename in {"sources.json", "source-coverage.json"}:
            continue
        if not isinstance(value, dict) or "fingerprint" not in value:
            raise RuntimeError(f"missing stored fingerprint in state/{filename}")
        excluded = {"observed_at", "fingerprint"}
        if filename == "freshness.json":
            excluded.update({"last_successful_observation", "last_changed"})
        payload = {key: item for key, item in value.items() if key not in excluded}
        if sha256_json(payload) != value["fingerprint"]:
            raise RuntimeError(f"stored fingerprint disagrees with state/{filename}")


def sdk_evidence_stream(source_id: str) -> str | None:
    for kind, repo, registry in (
        ("python", "typesafe-ai/typesafe-sdk-python", "registry:pypi:typesafe-sdk"),
        ("javascript", "typesafe-ai/typesafe-sdk-js", "registry:npm:@typesafe-ai/sdk"),
    ):
        if source_id.startswith(f"github:{repo}:") or source_id == registry:
            return kind
    return None


def retain_changelog_citations(snapshot, records, previous_records):
    """Keep historical mutable-doc citations distinct from current navigation."""
    retained = copy.deepcopy(snapshot)
    changed = False
    for release in retained.get("release_history", []):
        for evidence in release.get("documented_changes_provenance", []):
            sid = evidence["source_id"]
            current = records.get(sid)
            if current and current["sha256"] == evidence["sha256"]:
                continue
            previous = previous_records.get(sid)
            if not previous or previous["sha256"] != evidence["sha256"]:
                raise RuntimeError("retained SDK changelog citation has no matching source record")
            identity = (
                sid
                if previous["source_type"] == "historical-documentation"
                else f"retained:{sid}:{evidence['sha256']}"
            )
            records[identity] = {
                **previous,
                "source_id": identity,
                "source_type": "historical-documentation",
                "original_source_id": previous.get("original_source_id", sid),
            }
            if identity != sid:
                evidence["source_id"] = identity
                changed = True
    if not changed:
        return snapshot
    payload = {
        key: value for key, value in retained.items() if key not in {"observed_at", "fingerprint"}
    }
    return observed_state(payload, snapshot)


def synchronize(
    root: Path = ROOT,
    client: HTTPClient | None = None,
    strict: bool = False,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempt = attempt if attempt is not None else {}
    attempt.update(phase="recovery", publication_eligible=False)
    for key in ("health_errors", "last_successful_fetch", "last_successful_sync"):
        attempt.pop(key, None)
    with synchronization_lock(root):
        recover_pending(root)
        if (root / "events/baseline.json").exists():
            validate_tree(root, strict=True)
        return _synchronize(root, client, strict, attempt)


def _synchronize(
    root: Path,
    client: HTTPClient | None,
    strict: bool,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    client = _ObservedClient(client or HTTPClient(), attempt)
    attempt["phase"] = "discovery"
    previous_docs_manifest = read_json(root / "sources/docs.typesafe.ai/manifest.json", {}) or {}
    previous_github_manifest = (
        read_json(root / "sources/github.typesafe-ai/manifest.json", {}) or {}
    )
    previous_coverage = read_json(root / "state/source-coverage.json", {}) or {}
    previous_freshness = read_json(root / "state/freshness.json", {}) or {}
    previous_sources = read_json(root / "state/sources.json", {}) or {}
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
        intentionally_excluded = parse_llms_exclusions(index_response.body.decode("utf-8"))
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
            except FetchError as exc:
                # Only a missing Markdown representation justifies the HTML form. A
                # transient Markdown failure must not swap in different bytes and
                # report the page as modified.
                if exc.status != 404:
                    raise
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
        skill_commit = skill_commits[0]["sha"]
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
    sdk_candidates = {"python": python, "javascript": javascript}
    held_streams = {
        kind
        for kind, candidate in sdk_candidates.items()
        if candidate["discrepancies"]
        and previous_state.get(f"sdk-{kind}") is not None
        and not previous_state[f"sdk-{kind}"].get("discrepancies")
    }
    for kind, candidate in sdk_candidates.items():
        state[f"sdk-{kind}"] = previous_state[f"sdk-{kind}"] if kind in held_streams else candidate
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
    # One record per artifact, shared by the family manifests and state/sources.json,
    # so both files always carry the same observation metadata.
    records = {}
    for sid, artifact in sorted(artifacts.items()):
        identity = f"candidate:{sid}" if sdk_evidence_stream(sid) in held_streams else sid
        family = old_docs if artifact.source_type in DOCS_SOURCE_TYPES else old_github
        record = artifact_manifest(artifact, family.get(identity), *descriptions.get(sid, ("", "")))
        records[identity] = {**record, "source_id": identity}
    held_metadata = {}
    for kind in sorted(held_streams):
        held_metadata[kind] = {
            "state_retained": True,
            "retained_version": state[f"sdk-{kind}"]["version"],
            "candidate_tag_bindings": {
                release["tag"]: release["source_commit"]
                for release in sdk_candidates[kind]["release_history"]
                if release.get("tag") is not None and release.get("source_commit") is not None
            },
            "candidate_provenance": [
                {"source_id": sid, "url": record["fetch_url"], "sha256": record["sha256"]}
                for sid, record in records.items()
                if sid.startswith("candidate:")
                and sdk_evidence_stream(sid.removeprefix("candidate:")) == kind
            ],
        }
        records.update(
            {sid: record for sid, record in old_github.items() if sdk_evidence_stream(sid) == kind}
        )
        state[f"sdk-{kind}"] = retain_changelog_citations(
            state[f"sdk-{kind}"], records, {**old_github, **old_docs}
        )
    records = dict(sorted(records.items()))
    docs_items = [
        record for record in records.values() if record["source_type"] in DOCS_SOURCE_TYPES
    ]
    github_items = [
        record for record in records.values() if record["source_type"] not in DOCS_SOURCE_TYPES
    ]
    new_docs_manifest = {"schema_version": SCHEMA, "artifacts": docs_items}
    old_events_state = {name: previous_state.get(name) for name in state}
    old_events_state["sources"] = previous_sources
    baseline_exists = (root / "events/baseline.json").exists()
    candidates = (
        source_events(previous_docs_manifest, new_docs_manifest, observed)
        if baseline_exists
        else []
    )
    if baseline_exists:
        candidates.extend(semantic_events(old_events_state, state, observed))
    historical = load_history_events(root / "events")
    new_events = deduplicate_events(
        chain_events(candidates, historical), {event["id"] for event in historical}
    )
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
                **sdk_freshness(python, previous_freshness.get("python", {}), previous_freshness),
                **held_metadata.get("python", {}),
            },
            "javascript": {
                **sdk_freshness(
                    javascript, previous_freshness.get("javascript", {}), previous_freshness
                ),
                **held_metadata.get("javascript", {}),
            },
            "skill": {"status": "observed", "commit": skill_commit},
            "grace_period_days": 3,
        },
        previous_freshness,
    )
    if freshness["fingerprint"] == previous_freshness.get("fingerprint"):
        freshness["last_changed"] = previous_freshness.get(
            "last_changed", previous_freshness.get("last_successful_observation", observed)
        )
    else:
        freshness["last_changed"] = observed
    health_errors = actionable_discrepancies(freshness, utc_now())
    attempt["health_errors"] = health_errors
    sources = {
        "schema_version": SCHEMA,
        "scope": SOURCE_SCOPE,
        "documentation_license_note": DOCUMENTATION_LICENSE_NOTE,
        "artifacts": list(records.values()),
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
                "source": GITHUB_SOURCE_ROOT,
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
        validate_tree(stage, strict=True, history_root=root)
        attempt["phase"] = "promotion"
        promote(stage, root)
        attempt["phase"] = "sync-complete"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    # Publication is allowed only after the complete, internally consistent tree is
    # validated and promoted. Health failure stays visible via exit status/diagnostics.
    attempt["publication_eligible"] = True
    if strict and health_errors:
        names = ", ".join(health_errors)
        raise RuntimeError(
            "strict sync health failed; SDK release discrepancy persisted beyond grace period: "
            f"{names}; validated independent evidence published, unresolved SDK evidence remains explicit"
        )
    attempt["last_successful_sync"] = utc_now().isoformat().replace("+00:00", "Z")
    return {
        "pages": len(discovered),
        "artifacts": len(artifacts),
        "practices": len(state["practices"]["items"]),
        "events": len(new_events),
        "day": day,
        "health_errors": health_errors,
        "warnings": state["api"].get("warnings", []),
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
            with synchronization_lock(ROOT):
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
