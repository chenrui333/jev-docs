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


class _MarkdownDepthError(ValueError):
    """Source structure exceeds the supported nesting bound."""


def markdown_tokens(content: str):
    """Parse source structure, rejecting depth exhaustion before partial tokens escape."""
    # Only source collection needs the tokenizer. Read-only publication validation
    # remains standard-library-only on its Python 3.12 runner.
    from markdown_it import MarkdownIt

    def reject_deep(state, _start_line, _end_line, _silent):
        if state.level >= 64:
            raise _MarkdownDepthError
        return False

    # The native limit silently truncates before emitting a token at that level.
    # Keep it above our explicit guard so unsupported input cannot look complete.
    parser = MarkdownIt("commonmark", {"html": False, "maxNesting": 256})
    parser.block.ruler.before("code", "jev_docs_depth", reject_deep)
    try:
        return parser.parse(content)
    except _MarkdownDepthError:
        return None


def without_code_examples(content: str) -> str:
    """Retain raw prose/scalar spans; discard common Markdown/HTML code forms."""
    # Markdown examples are data: an unmatched HTML tag/comment inside one
    # must not consume active prose after the closing fence.
    tokens = markdown_tokens(content)
    if tokens is None:
        return ""
    excluded = set()
    for token in tokens:
        if token.map and token.type in {"fence", "code_block", "blockquote_open"}:
            excluded.update(range(*token.map))
    content = "".join(
        "\n" if line_number in excluded else line
        for line_number, line in enumerate(content.splitlines(keepends=True))
    )
    content = without_comments(content)
    content = re.sub(
        r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)",
        lambda match: (
            match[0]
            if match[2].strip()
            and not re.search(r"[<>\s]", match[2].strip())
            and not re.search(r"[\r\n]", match[2])
            else "\n\n"
        ),
        content,
        flags=re.S,
    )
    containers: list[str] = []
    parts: list[str] = []
    position = 0
    for tag in re.finditer(r"<\s*(/?)\s*(code|pre|script|style)\b[^>]*>", content, re.I):
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
    return "".join(parts)
