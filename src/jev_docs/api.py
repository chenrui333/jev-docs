"""Narrow, claim-level extraction of the official HTTP reference, not examples."""

import copy
import html
import re

FIELDS = (
    "endpoint",
    "request_fields",
    "required_fields",
    "model_required",
    "question_types",
    "model_default",
    "response_shape",
)


def section(text, title):
    match = re.search(rf"^## {re.escape(title)}\s*$([\s\S]*?)(?=^## |\Z)", text, re.M)
    return match[1] if match else ""


def attributes(tag):
    return {key: html.unescape(value) for key, value in re.findall(r'([\w-]+)="([^"]*)"', tag)}


def field_tags(text, component):
    # A quoted type (e.g. map<string, Answer>) can itself contain '>'.
    return re.findall(rf'<{component}\b((?:"[^"]*"|[^">])*)>', text)


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
        verified = value is not None and bool(passages) and bool(provenance)
        item = {
            "status": "verified" if verified else "unknown",
            "value": value if verified else None,
            "method": "official_reference_extraction",
            "evidence": [{**provenance[0], "excerpt": p[:240]} for p in passages]
            if verified
            else [],
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

    endpoint = section(text, "Evaluation endpoint")
    lines = re.findall(r"^(POST https://[^\s`]+)\s*$", endpoint, re.M)
    fact("endpoint", lines[0] if len(set(lines)) == 1 else None, lines)
    body = section(text, "Request body")
    declarations = {}
    # Top-level request entries are outside nested Expandable blocks.
    top = re.sub(r"<Expandable\b[\s\S]*?</Expandable>", "", body)
    tags = field_tags(top, "ParamField")
    for tag in tags:
        attrs = attributes(tag)
        name = attrs.get("body")
        if name:
            required = bool(re.search(r"(?:^|\s)required(?:\s|$|/)", tag))
            declarations.setdefault(name, []).append((required, attrs, "<ParamField" + tag + ">"))
    consistent = declarations and all(
        len({row[0] for row in rows}) == 1 for rows in declarations.values()
    )
    passages = [row[2] for rows in declarations.values() for row in rows]
    fact("request_fields", sorted(declarations) if consistent else None, passages)
    fact(
        "required_fields",
        sorted(name for name, rows in declarations.items() if rows[0][0]) if consistent else None,
        passages,
    )
    model = declarations.get("model", [])
    model_ok = bool(model) and len({(r[0], r[1].get("default")) for r in model}) == 1
    fact("model_required", model[0][0] if model_ok else None, [r[2] for r in model])
    default = model[0][1].get("default") if model_ok else None
    # Required and default simultaneously is contradictory, not permission to omit.
    fact(
        "model_default",
        default if model_ok and not model[0][0] else None,
        [r[2] for r in model],
        "No explicit optional model default is documented; recommended/example aliases are not defaults.",
    )
    questions = section(text, "Question types")
    type_tags = [(tag, attributes(tag)) for tag in field_tags(questions, "ParamField")]
    declarations = [
        (tag, attrs["type"].strip('"'))
        for tag, attrs in type_tags
        if attrs.get("body") == "type"
        and re.fullmatch(r'"[a-z][a-z0-9_-]*"', attrs.get("type", ""))
    ]
    types = sorted({name for _, name in declarations})
    fact(
        "question_types",
        types or None,
        ["<ParamField" + tag + ">" for tag, _ in declarations],
    )
    response = section(text, "Response body")
    answers = [
        tag
        for tag in field_tags(response, "ResponseField")
        if attributes(tag).get("name") == "answers"
    ]
    correlation = re.search(r"[^.\n<>]*same (?:ids|keys)[^.\n<>]*\.?", response, re.I)
    shape = (
        "answers map keyed by question id"
        if len(answers) == 1
        and attributes(answers[0]).get("type", "").startswith("map<")
        and correlation
        else None
    )
    fact(
        "response_shape",
        shape,
        ["<ResponseField" + tag + ">" for tag in answers]
        + ([correlation[0].strip()] if correlation else []),
    )
    return {
        "schema_version": 1,
        **{key: properties[key]["value"] for key in FIELDS},
        "properties": properties,
        "status_vocabulary": ["verified", "inferred", "unknown"],
        "interpretation": "Only explicit reference declarations are verified. Inferred would mean example-derived; no examples establish defaults or requiredness.",
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
