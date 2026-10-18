"""Small deterministic text helpers, not a Markdown renderer."""

import html
import re


def clean_text(content: str) -> str:
    # Only presentation tags: preserve comparisons and unknown angle-bracket text.
    content = re.sub(r"<!--.*?-->", "", content, flags=re.S)
    # Preserve prose block boundaries inside Mintlify JSX demos.
    content = re.sub(r"</?(?:p|div|details|summary|section)\b[^>]*>", "\n\n", content)
    content = re.sub(r"^(\s*)\* ", r"\1- ", content, flags=re.M)
    content = re.sub(
        r"</?(?:Note|Tip|Info|Warning|Accordion|AccordionGroup|Card|CardGroup|Steps|Step|Tabs|Tab|div|span|p|strong|em|a|br|code)\b[^>]*>",
        " ",
        content,
    )
    content = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", content)
    content = re.sub(r"\*\*([^*]+)\*\*", r"\1", content)
    content = re.sub(r"(?<!\w)\*([^*\s][^*]*?[^*\s]|[^*\s])\*(?!\w)", r"\1", content)
    content = content.replace("`", "")
    return html.unescape(content)


def excerpt(content: str, pattern: str, limit: int = 420) -> str | None:
    """Prefer complete matching paragraphs, list items or sentences; never crop words."""
    content = clean_text(content)
    match = re.search(pattern, content, re.I | re.M)
    if not match:
        return None
    # Blank lines and list starts bound blocks; wrapped prose stays together.
    for block in re.split(r"\n\s*\n|\n(?=\s*[-*] )", content):
        block = re.sub(r"^\s*(?:[-*>]|#+)\s+", "", block).strip()
        block = re.sub(r"\s+", " ", block)
        if not re.search(pattern, block, re.I):
            continue
        if len(block) <= limit:
            return block
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z`\"'])", block)
        for sentence in sentences:
            if re.search(pattern, sentence, re.I):
                if len(sentence) <= limit:
                    return sentence
                # Explicit omission; keep the beginning and matching claim if possible.
                clipped = sentence[: limit - 2].rsplit(" ", 1)[0]
                if re.search(pattern, clipped, re.I):
                    return clipped.rstrip() + " …"
                return None
    return None


def without_comments(content: str) -> str:
    """Discard inactive HTML comments, including an unclosed trailing comment."""
    return re.sub(r"<!--[\s\S]*?(?:-->|\Z)", "\n", content)


def without_code_examples(content: str) -> str:
    """Retain raw prose/scalar spans; discard common Markdown/HTML code forms."""
    content = without_comments(content)
    containers: list[str] = []
    parts: list[str] = []
    position = 0
    for tag in re.finditer(r"<\s*(/?)\s*(code|pre)\b[^>]*>", content, re.I):
        if not containers:
            parts.append(content[position : tag.start()])
        if tag[1]:
            if containers and containers[-1] == tag[2].lower():
                containers.pop()
        elif not tag[0].rstrip().endswith("/>"):
            containers.append(tag[2].lower())
        parts.append("\n\n")
        position = tag.end()
    if not containers:
        parts.append(content[position:])
    prose = []
    fence = None
    for line in "".join(parts).splitlines(keepends=True):
        if re.match(r" {0,3}>", line):
            # Quoted examples are not affirmative reference declarations, with or
            # without an inner fence (including tilde and nested quote forms).
            prose.append("\n")
            continue
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            prose.append("\n")
        elif marker:
            fence = marker[1][0], len(marker[1])
            prose.append("\n")
        elif line.expandtabs(4).startswith("    "):
            prose.append("\n")
        else:
            prose.append(line)
    return re.sub(
        r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)",
        lambda match: (
            match[0]
            if match[2].strip()
            and not re.search(r"\s", match[2].strip())
            and not re.search(r"[\r\n]", match[2])
            else "\n\n"
        ),
        "".join(prose),
        flags=re.S,
    )
