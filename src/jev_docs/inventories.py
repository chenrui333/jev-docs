"""Conservative inventories extracted from official documentation structure."""

import copy
import hashlib
import re
from urllib.parse import urlsplit

from jev_docs.text import clean_text


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def provenance(artifact, section=None):
    return [
        {
            "source_id": artifact.source_id,
            "url": artifact.url + (f"#{section}" if section else ""),
            "sha256": artifact.sha256,
        }
    ]


# Independently authored interpretations of reviewed, model-specific evidence.
# Exact section hashes prevent a familiar heading from endorsing changed prose.
_LIMITATION_INTERPRETATIONS = {
    "adversarial-content": (
        "55575a729128f6bbe95d0838d34ea232cc90955469a11aba1e77d3d1eb599559",
        "Untrusted state may influence decisions; specify adversarial cases in criteria and test the integration.",
    ),
    "choice-option-order": (
        "34dc9e81163307bf55f1bebcbf9bcda15c751dbf990b06674644eba6ecac2967",
        "Choice results may depend on option position. Check decisions after permuting candidates.",
    ),
    "common-sense-structural-invariants": (
        "f5d18200f162450a87dba7a85f0d0a0b8c3fd94d665e008ff917cc8b3d62ea8f",
        "Similar wording does not guarantee algebraic relationships across questions. Calibrate Choice and Noul decisions separately.",
    ),
    "contradictory-instructions-and-criteria": (
        "7f5456c18c04af6e6c4f5bd2364cecf33670e07694c4f7331ea96f79bf84b9a1",
        "Conflicting instructions and criteria can impair decisions; make their requirements agree.",
    ),
    "counting": (
        "88eeb3d33ed56e2abd0b5535bc06f64ddbb5a964815b9d81db81dc495b68e32e",
        "Do not delegate tallying to this model. Classify individual candidates if needed, then aggregate programmatically.",
    ),
    "date-and-time-comparison": (
        "6b530f8d3222d718ed74cbc5eec8cc1272166d17758743abdabc02d0d7e7b7f4",
        "Date text is not reliably ordered by the model. Separate semantic extraction from programmatic date calculations.",
    ),
    "generation": (
        "909fe1da53fa433f3dd367dcab2d5b5119e6b7865ce6095a0086a81cffa72c31",
        "Free-text production is outside this model's training. For bounded extraction, obtain candidates elsewhere and use Choice to select.",
    ),
    "indirection": (
        "c55e59fc8850bd46b652d644b75775216b182c700124475cd125c4640dbedaa4",
        "Nested reasoning and double negation reduce reliability. Ask directly about explicitly named state fields.",
    ),
    "large-state-full-of-irrelevant-detail": (
        "2c143c92dc1d093ab96ead10fae277fa744e9e32674381cdfaea750b9b35125b",
        "Irrelevant input can reduce accuracy and obscure error causes. Select relevant fields before evaluation.",
    ),
    "literal-reading": (
        "77fc11629b076dfbd74ab69cbde6bb42a75c60ef250beb0888ec51bf50d8930d",
        "Implicit intent may not be followed. Specify exact conditions and boundary cases, splitting ambiguous decisions when needed.",
    ),
    "math-and-numbers": (
        "8ece3ff21025bd592c76709de12f7015f5ab7acdd343131167c5a7adfc0d96d8",
        "Reserve the model for semantic judgments; implement numerical operations programmatically.",
    ),
    "math-using-score": (
        "afc388f9a43dd79780da058f7389457d593ad6637370c04250dcba3c76e3d52f",
        "A score is not an exact numerical measurement interpolated between criterion levels.",
    ),
    "numeric-representations": (
        "722c47f75f08f4f40eec13a4fe5266cba6d8568beb657a13109a40c888024ead",
        "Encoded numeric values are weaker inputs than semantic labels. Perform conversions outside the model before asking for judgment.",
    ),
}


def _limitation_summary(model, section, section_hash):
    reviewed = _LIMITATION_INTERPRETATIONS.get(section)
    if model == "jev-1.13" and reviewed and reviewed[0] == section_hash:
        return reviewed[1], "repository-interpretation"
    return (
        "Unknown: this section has no reviewed repository interpretation. Consult the official evidence.",
        "unknown",
    )


def model_limitations(contents, artifacts, previous):
    items = {}
    for sid, content in sorted(contents.items()):
        if not sid.startswith("docs:model-jaggedness/"):
            continue
        applies = re.search(r"Applies to\s+`(jev-[\d.]+)`", content)
        reviewed = re.search(r"Last reviewed (\d{4}-\d{2}-\d{2})", content)
        if not applies or not reviewed or sid.rsplit("/", 1)[1] != applies[1]:
            raise RuntimeError(f"unrecognized model applicability: {sid}")
        model = applies[1]
        headings = list(re.finditer(r"^(#{2,3}) (.+)$", content, re.M))
        count = 0
        for i, heading in enumerate(headings):
            title = heading[2].strip()
            if title == "The failure modes in detail":
                continue
            section = slug(title)
            body = content[
                heading.end() : headings[i + 1].start() if i + 1 < len(headings) else len(content)
            ]
            body = re.split(r"```|<Info>|<Tip>", body)[0].strip()
            if not clean_text(body).strip():
                raise RuntimeError(f"empty limitation section: {sid}#{section}")
            section_hash = hashlib.sha256(
                re.sub(r"\s+", " ", clean_text(body)).strip().encode()
            ).hexdigest()
            summary, summary_kind = _limitation_summary(model, section, section_hash)
            ident = f"{model}:{section}"
            items[ident] = {
                "id": ident,
                "model": model,
                "section": section,
                "title": title,
                "last_reviewed": reviewed[1],
                "status": "documented",
                "summary": summary,
                "summary_kind": summary_kind,
                "section_text_sha256": section_hash,
                "mitigation": None,
                "provenance": provenance(artifacts[sid], section),
            }
            count += 1
        if not count:
            raise RuntimeError(f"empty limitations page: {sid}")
    # Absence is not evidence that a model weakness is fixed. Retain prior evidence.
    for old in (previous or {}).get("items", []):
        if old["id"] not in items:
            retained = copy.deepcopy(old)
            retained["status"] = "unknown"
            retained["summary"], retained["summary_kind"] = _limitation_summary(
                old["model"], old["section"], old.get("section_text_sha256")
            )
            retained["mitigation"] = None
            items[old["id"]] = retained
    return {
        "schema_version": 1,
        "scope": "Upstream-documented limitations for specific model versions; absence does not establish a fix.",
        "items": [items[key] for key in sorted(items)],
    }


def cookbooks(contents, artifacts, discovered, previous):
    sid = "docs:cookbooks"
    indexed = {}
    if sid in contents:
        category = None
        for line in contents[sid].splitlines():
            if line.startswith("## "):
                category = slug(line[3:].strip())
            if not line.lstrip().startswith("| ["):
                continue
            cells = [part.strip() for part in line.strip().strip("|").split("|")]
            match = re.fullmatch(r"\[([^]]+)\]\(([^)]+)\)", cells[0])
            if (
                len(cells) != 3
                or not match
                or not category
                or cells[2] not in {"Beginner", "Intermediate", "Advanced"}
            ):
                raise RuntimeError("unrecognized cookbook index row")
            url = match[2]
            if url.startswith("/"):
                url = "https://docs.typesafe.ai" + url
            parts = urlsplit(url)
            if parts.netloc != "docs.typesafe.ai" or not parts.path.startswith("/cookbooks/"):
                raise RuntimeError("noncanonical cookbook index link")
            ident = parts.path.removesuffix(".md").lstrip("/")
            if ident in indexed:
                raise RuntimeError("duplicate cookbook index entry")
            indexed[ident] = {
                "id": ident,
                "title": match[1],
                "category": category,
                "level": cells[2],
                "url": "https://docs.typesafe.ai/" + ident,
                "summary": clean_text(cells[1]),
                "indexed": True,
                "provenance": provenance(artifacts[sid]),
            }
        if not indexed:
            raise RuntimeError("empty or malformed cookbook index")
    elif previous and previous.get("items"):
        raise RuntimeError("cookbook index disappeared; inventory not promoted")
    for url, title, description in discovered:
        ident = urlsplit(url).path.removesuffix(".md").lstrip("/")
        if not ident.startswith("cookbooks/"):
            continue
        item = indexed.setdefault(
            ident,
            {
                "id": ident,
                "title": title,
                "category": None,
                "level": None,
                "url": "https://docs.typesafe.ai/" + ident,
                "summary": description,
                "indexed": False,
                "provenance": [],
            },
        )
        item["discovered"] = True
        item["provenance"] += provenance(artifacts["docs:" + ident])
    for item in indexed.values():
        item.setdefault("discovered", False)
    return {
        "schema_version": 1,
        "scope": "Official cookbook navigation; index membership and canonical discovery are independent. Descriptions are upstream claims, not independently verified benchmarks.",
        "items": [indexed[key] for key in sorted(indexed)],
    }


def render_limitations(state):
    lines = [
        "# Model-specific Jev limitations",
        "",
        state["scope"],
        "",
        "Generated from [structured state](state/model-limitations.json). These describe the named version, not all present or future Jev models.",
        "",
    ]
    for item in state["items"]:
        summary, summary_kind = _limitation_summary(
            item["model"], item["section"], item.get("section_text_sha256")
        )
        # Preserve the historical rendering format until synchronization upgrades
        # this item with the additive interpretation marker.
        legacy = "summary_kind" not in item
        lines += [
            f"## {item['model']} / {item['title']}",
            "",
            f"Status: `{item['status']}`. Upstream last reviewed: {item['last_reviewed']}.",
            "",
            item["summary"] if legacy else f"Repository interpretation ({summary_kind}): {summary}",
            "",
        ]
        if legacy and item["mitigation"]:
            lines += ["Upstream mitigation: " + item["mitigation"], ""]
        lines += [f"[Official evidence]({item['provenance'][0]['url']})", ""]
    return "\n".join(lines)
