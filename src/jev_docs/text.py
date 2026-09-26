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
