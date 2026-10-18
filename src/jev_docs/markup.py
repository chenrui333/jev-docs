"""Source-position HTML/MDX markers; no rendering or JavaScript evaluation."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True, slots=True)
class _MarkupToken:
    kind: str
    name: str
    raw: str
    start: int
    end: int
    escaped: bool = False


def _escaped(text, position):
    start = position
    while start and text[start - 1] == "\\":
        start -= 1
    return (position - start) % 2 != 0


def _markup(text):
    class Markup(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.tokens = []
            self.lines = [0] + [match.end() for match in re.finditer("\n", text)]

        def position(self):
            line, column = self.getpos()
            return self.lines[line - 1] + column

        def opening(self, kind, name):
            start = self.position()
            raw = self.get_starttag_text()
            self.tokens.append(
                _MarkupToken(kind, name, raw, start, start + len(raw), _escaped(text, start))
            )

        def handle_starttag(self, name, _attrs):
            self.opening("start", name)

        def handle_startendtag(self, name, _attrs):
            self.opening("empty", name)

        def handle_endtag(self, name):
            start = self.position()
            end = text.find(">", start) + 1
            self.tokens.append(
                _MarkupToken("end", name, text[start:end], start, end, _escaped(text, start))
            )

        def handle_data(self, data):
            start = self.position()
            self.tokens.append(_MarkupToken("data", "", data, start, start + len(data)))

        def handle_comment(self, data):
            start = self.position()
            end = text.find(">", start + 4 + len(data)) + 1
            self.tokens.append(
                _MarkupToken("meta", "comment", data, start, end, _escaped(text, start))
            )

    parser = Markup()
    parser.feed(text)
    incomplete = bool(parser.rawdata.strip())
    if incomplete:
        start = len(text) - len(parser.rawdata)
        parser.tokens.append(
            _MarkupToken("incomplete", "", parser.rawdata, start, len(text), _escaped(text, start))
        )
    parser.close()
    return parser.tokens, incomplete or bool(parser.rawdata.strip())
