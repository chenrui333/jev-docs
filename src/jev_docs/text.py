"""Small deterministic text helpers, not a Markdown renderer."""

import html
import re

from jev_docs.markup import _markup


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
    """Discard actual comments, never comment markers in element metadata."""
    tokens, _incomplete = _markup(content)
    parts = []
    position = 0
    for token in tokens:
        if token.escaped or not (
            token.kind == "meta"
            and token.name == "comment"
            or token.kind == "incomplete"
            and token.raw.startswith("<!--")
        ):
            continue
        if token.start >= position:
            parts.extend((content[position : token.start], "\n\n"))
        position = max(position, token.end)
    parts.append(content[position:])
    return "".join(parts)


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


def _without_markdown_blocks(
    content: str, kinds: set[str], *, keep_http: bool = False
) -> str | None:
    tokens = markdown_tokens(content)
    if tokens is None:
        return None
    lines = content.splitlines(keepends=True)
    excluded = set()
    for token in tokens:
        if token.map and token.type in kinds:
            if (
                keep_http
                and token.type == "fence"
                and token.level == 0
                and token.info.split(maxsplit=1)[:1] == ["http"]
                and re.fullmatch(
                    r"POST https://[^\s`]+(?:\n[A-Za-z0-9-]+:[^\r\n]*)*", token.content.strip()
                )
            ):
                start, end = token.map
                closing = re.escape(token.markup[0]) + "{" + str(len(token.markup)) + ",}"
                if end <= start + 1 or not re.fullmatch(closing, lines[end - 1].strip()):
                    return None
                excluded.update((start, end - 1))
            else:
                excluded.update(range(*token.map))
    return "".join(
        "\n" if line_number in excluded else line for line_number, line in enumerate(lines)
    )


def without_code_examples(content: str, *, keep_http: bool = False) -> str:
    """Retain raw prose/scalar spans; discard common Markdown/HTML code forms."""
    # Markdown examples are data: an unmatched HTML tag/comment inside one
    # must not consume active prose after the closing fence.
    content = _without_markdown_blocks(content, {"fence", "code_block"}, keep_http=keep_http)
    if content is None:
        return ""
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
    tokens, _incomplete = _markup(content)
    blocked = {"code", "pre", "script", "style", "blockquote"}
    for token in tokens:
        if token.kind == "incomplete" and re.match(
            r"</?(?:code|pre|script|style|blockquote)\b", token.raw, re.I
        ):
            return ""
        if token.name not in blocked or token.escaped:
            continue
        if not containers:
            parts.append(content[position : token.start])
        if token.kind == "start":
            containers.append(token.name)
        elif token.kind == "end" and containers and containers[-1] == token.name:
            containers.pop()
        parts.append("\n\n")
        position = token.end
    if not containers:
        parts.append(content[position:])
    # Keep quoted HTML delimiters through the scan: removing only the opener
    # could expose otherwise quoted/container-scoped prose as active evidence.
    return _without_markdown_blocks("".join(parts), {"blockquote_open"}) or ""


def without_link_metadata(content: str) -> str:
    """Exclude links/images/reference definitions from contract declaration scans."""
    from markdown_it import MarkdownIt
    from markdown_it.rules_block import reference
    from markdown_it.rules_inline import autolink, image, link

    parser = MarkdownIt("commonmark", {"html": False, "maxNesting": 64})
    spans = []
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def record_reference(state, start, end, silent):
        matched = reference(state, start, end, silent)
        if matched and not silent:
            spans.append((offsets[start], offsets[state.line]))
        return matched

    alternatives = [
        name
        for name in ("paragraph", "reference", "blockquote", "list")
        if reference in parser.block.ruler.getRules(name)
    ]
    parser.block.ruler.at("reference", record_reference, {"alt": alternatives})
    env = {}
    parser.parse(content, env)

    for name, rule in (("link", link), ("image", image), ("autolink", autolink)):

        def record_link(state, silent, original=rule):
            start = state.pos
            matched = original(state, silent)
            if matched and not silent:
                spans.append((start, state.pos))
            return matched

        parser.inline.ruler.at(name, record_link)
    parser.parseInline(content, env)
    position = 0
    pieces = []
    for start, end in sorted(spans):
        if start >= position:
            pieces.append(content[position:start])
            pieces.append("\n")
        position = max(position, end)
    pieces.append(content[position:])
    return "".join(pieces)
