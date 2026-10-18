"""Narrow, claim-level extraction of the official HTTP reference, not examples."""

import copy
import hashlib
import html
import re

from jev_docs.text import without_code_examples, without_comments

FIELDS = (
    "endpoint",
    "request_fields",
    "required_fields",
    "model_required",
    "question_types",
    "model_default",
    "response_shape",
)

# Reviewed from https://docs.typesafe.ai/api.md, retained page SHA-256
# 37f0f3a41917e17592876f67848bd63329ba576081a89704f28210f0efc3c0f6.
# Hash the COMPLETE Response body section, including examples, after normalizing
# line endings, trailing line whitespace, and outer whitespace only.
REVIEWED_RESPONSE_SECTION_SHA256 = (
    "b677797741c142fc0d509bf216bee079dea75dc556b8730a2898a600ac9e8924"
)
REVIEWED_RESPONSE_EVIDENCE = (
    '<ResponseField name="answers" type="map<string, Answer>" required>',
    "One answer per question, returned under the same ids you provided.",
)


def section(text, title):
    matches = list(re.finditer(rf"^## {re.escape(title)}\s*$([\s\S]*?)(?=^## |\Z)", text, re.M))
    # Duplicate authoritative sections are conflicting context, not first-one-wins.
    return matches[0][1] if len(matches) == 1 else ""


def without_expandables(text):
    """Remove complete nested map-entry descriptions, rejecting unbalanced markup."""
    chunks = []
    depth = 0
    end = 0
    for match in re.finditer(r'<(/?)Expandable\b(?:"[^"]*"|[^">])*>', text, re.I):
        if depth == 0:
            chunks.append(text[end : match.start()])
        if match[1]:
            depth -= 1
            if depth < 0:
                return None
        elif not match[0].rstrip().endswith("/>"):
            depth += 1
        end = match.end()
    if depth:
        return None
    chunks.append(text[end:])
    return "".join(chunks)


def attributes(tag):
    """Accept the reference's narrow attribute syntax, never partial attributes."""
    attrs = {}
    remaining = tag.strip().removesuffix("/").rstrip()
    while remaining:
        token = re.match(r'([\w-]+)(?:\s*=\s*("[^"]*"|\{[^{}]*\}))?(?=\s|$)', remaining)
        if not token or token[1] in attrs:
            return None
        key, raw = token[1], token[2]
        if key == "required":
            attrs[key] = {None: True, "{true}": True, "{false}": False}.get(raw)
        elif raw is not None and raw.startswith('"'):
            attrs[key] = html.unescape(raw[1:-1])
        else:
            return None
        remaining = remaining[token.end() :].lstrip()
    return attrs or None


def field_tags(text, component):
    # Quoted types and unsupported expressions can themselves contain '>'.
    tags = re.findall(rf"""<{component}\b((?:"[^"]*"|'[^']*'|\{{[^{{}}]*\}}|[^"'{{>])*)>""", text)
    # An unparsed opening must not silently shrink an otherwise verified list.
    if len(tags) != len(re.findall(rf"<{component}\b", text)):
        tags.append("")
    return tags


def extract_api(contents, artifacts, previous=None):
    """Keep flat values for readers; properties explain evidence and uncertainty.

    Only the canonical reference establishes the contract. Examples and migration
    pages do not override it. Missing/ambiguous claims are unknown, not defaults.
    """
    previous = previous or {}
    sid = "docs:api"
    artifact = artifacts.get(sid)
    text = contents.get(sid, "") if artifact else ""
    provenance = []
    warnings = []
    properties = {}
    if text:
        provenance = [{"source_id": sid, "url": artifact.url, "sha256": artifact.sha256}]
    else:
        warnings.append(
            "API reference docs:api is missing; restore canonical discovery before relying on current API claims."
        )
    missing = sorted(
        {
            s["source_id"]
            for s in previous.get("provenance", [])
            if s.get("source_id") not in artifacts
        }
    )
    # Preserve disappeared references even after the first unknown observation.
    missing = sorted(
        set(missing)
        | {
            s["source_id"]
            for s in previous.get("missing_sources", [])
            if s["source_id"] not in artifacts
        }
    )
    old_sources = {
        s["source_id"]: s
        for s in previous.get("provenance", []) + previous.get("missing_sources", [])
    }
    for source in missing:
        warnings.append(
            f"Previously cited API source {source} disappeared; retained as historical evidence, not current verification."
        )

    def fact(name, value, passages, reason=None):
        verified = (
            value is not None
            and bool(passages)
            and bool(provenance)
            and all(len(p) <= 240 and p in text for p in passages)
        )
        item = {
            "status": "verified" if verified else "unknown",
            "value": value if verified else None,
            "method": "official_reference_extraction",
            "evidence": [{**provenance[0], "excerpt": p} for p in passages] if verified else [],
        }
        if not verified:
            item["reason"] = (
                reason or "No unambiguous explicit declaration in the canonical reference."
            )
            old = previous.get("properties", {}).get(name)
            if old:
                retained = old if old.get("status") == "verified" else old.get("last_known")
                if retained:
                    item["last_known"] = copy.deepcopy(retained)
            elif previous.get(name) is not None:
                item["last_known"] = {
                    "value": previous[name],
                    "status": "curated_unverified",
                    "evidence": previous.get("provenance", []),
                }
            if name != "model_default" and text:
                warnings.append(
                    f"API property {name} is unknown; review the reference declaration before use."
                )
        properties[name] = item

    reference = without_code_examples(text)
    endpoint = (
        section(without_comments(text), "Evaluation endpoint")
        if re.search(r"^## Evaluation endpoint\s*$", reference, re.M)
        else ""
    )
    lines = re.findall(r"^(POST https://[^\s`]+)\s*$", endpoint, re.M)
    fact("endpoint", lines[0] if len(set(lines)) == 1 else None, lines)
    body = section(reference, "Request body")
    declarations = {}
    # Top-level request entries are outside nested Expandable blocks.
    top = without_expandables(body)
    tags = field_tags(top, "ParamField") if top is not None else []
    names_known = bool(tags)
    for tag in tags:
        attrs = attributes(tag)
        name = attrs.get("body") if attrs else None
        if name:
            required = attrs.get("required", False)
            declarations.setdefault(name, []).append((required, attrs, "<ParamField" + tag + ">"))
        else:
            names_known = False
    consistent = names_known and all(
        rows[0][0] is not None and len({row[0] for row in rows}) == 1
        for rows in declarations.values()
    )
    passages = [row[2] for rows in declarations.values() for row in rows]
    fact("request_fields", sorted(declarations) if names_known else None, passages)
    fact(
        "required_fields",
        sorted(name for name, rows in declarations.items() if rows[0][0]) if consistent else None,
        passages,
    )
    model = declarations.get("model", [])
    model_ok = (
        bool(model)
        and names_known
        and model[0][0] is not None
        and len({(r[0], r[1].get("default")) for r in model}) == 1
    )
    fact("model_required", model[0][0] if model_ok else None, [r[2] for r in model])
    default = model[0][1].get("default") if model_ok else None
    # Required and default simultaneously is contradictory, not permission to omit.
    fact(
        "model_default",
        default if model_ok and not model[0][0] else None,
        [r[2] for r in model],
        "No explicit optional model default is documented; recommended/example aliases are not defaults.",
    )
    questions = section(reference, "Question types")
    type_tags = [(tag, attributes(tag)) for tag in field_tags(questions, "ParamField")]
    declarations = [
        (tag, attrs["type"].strip('"'))
        for tag, attrs in type_tags
        if attrs
        and attrs.get("body") == "type"
        and re.fullmatch(r'"[a-z][a-z0-9_-]*"', attrs.get("type", ""))
    ]
    types_known = all(
        attrs is not None
        and (
            attrs.get("body") != "type"
            or re.fullmatch(r'"[a-z][a-z0-9_-]*"', attrs.get("type", ""))
        )
        for _, attrs in type_tags
    )
    types = sorted({name for _, name in declarations})
    fact(
        "question_types",
        types if types and types_known else None,
        ["<ParamField" + tag + ">" for tag, _ in declarations],
    )
    # Free-form prose is not a declaration grammar. This repository interpretation
    # applies only to a reviewed complete context, never familiar sentence fragments.
    response = section(text, "Response body")
    normalized_response = "\n".join(line.rstrip() for line in response.splitlines()).strip()
    response_sha256 = hashlib.sha256(normalized_response.encode()).hexdigest()
    reviewed = response_sha256 == REVIEWED_RESPONSE_SECTION_SHA256 and bool(
        re.search(r"^## Response body\s*$", reference, re.M)
    )
    fact(
        "response_shape",
        "answers map keyed by question id" if reviewed else None,
        REVIEWED_RESPONSE_EVIDENCE,
        "The Response body is not an active reviewed reference section; review it before relying on response correlation.",
    )
    properties["response_shape"].update(
        method="reviewed_reference_interpretation",
        interpretation="Explicit repository interpretation of a reviewed canonical Response body revision, not automatic prose extraction.",
        reviewed_section_sha256=REVIEWED_RESPONSE_SECTION_SHA256,
        observed_section_sha256=response_sha256,
    )
    return {
        "schema_version": 1,
        **{key: properties[key]["value"] for key in FIELDS},
        "properties": properties,
        "status_vocabulary": ["verified", "inferred", "unknown"],
        "interpretation": "Explicit reference declarations are extracted; response_shape is a repository interpretation gated by a reviewed complete section hash. Examples never establish defaults or requiredness.",
        "provenance": provenance,
        "missing_sources": [old_sources[s] for s in missing],
        "warnings": warnings,
    }


def validate_api(api, sources):
    # Legacy snapshots predate claim-level verification; do not retrofit certainty.
    if not api or "properties" not in api:
        return
    for name in FIELDS:
        item = api["properties"][name]
        if api.get(name) != item["value"]:
            raise RuntimeError(f"API value disagrees with property evidence: {name}")
        if item["status"] == "unknown":
            if item["value"] is not None:
                raise RuntimeError(f"Unknown API property has a current value: {name}")
            continue
        if item["status"] != "verified" or not item["evidence"]:
            raise RuntimeError(f"API property lacks explicit verification: {name}")
        for evidence in item["evidence"]:
            source = sources.get(evidence["source_id"], {})
            if not evidence.get("excerpt") or evidence.get("sha256") != source.get("sha256"):
                raise RuntimeError(f"API provenance does not match retained artifact: {name}")
