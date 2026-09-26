"""Conservative inventories extracted from official documentation structure."""

import copy
import hashlib
import re
from urllib.parse import urlsplit

from jev_docs.text import clean_text, excerpt


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
            summary = excerpt(body.split("**Instead:**")[0], r"\S", 300)
            mitigation = (
                excerpt(body.split("**Instead:**", 1)[1], r"\S", 650)
                if "**Instead:**" in body
                else None
            )
            if not summary:
                raise RuntimeError(f"empty limitation section: {sid}#{section}")
            ident = f"{model}:{section}"
            items[ident] = {
                "id": ident,
                "model": model,
                "section": section,
                "title": title,
                "last_reviewed": reviewed[1],
                "status": "documented",
                "summary": summary,
                "section_text_sha256": hashlib.sha256(
                    re.sub(r"\s+", " ", clean_text(body)).strip().encode()
                ).hexdigest(),
                "mitigation": mitigation,
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
        lines += [
            f"## {item['model']} / {item['title']}",
            "",
            f"Status: `{item['status']}`. Upstream last reviewed: {item['last_reviewed']}.",
            "",
            item["summary"],
            "",
        ]
        if item["mitigation"]:
            lines += ["Upstream mitigation: " + item["mitigation"], ""]
        lines += [f"[Official evidence]({item['provenance'][0]['url']})", ""]
    return "\n".join(lines)
