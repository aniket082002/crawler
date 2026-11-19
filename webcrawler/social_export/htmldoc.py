"""A small, tolerant HTML reader for Instagram's HTML exports.

Builds a lightweight element tree with the standard library's html.parser.
Broken markup never raises: unclosed and stray tags are tolerated, and the
tree is bounded in size and depth so a hostile file cannot exhaust memory.
Scripts are never run (and their text is ignored); nothing is fetched.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

MAX_NODES = 300_000
MAX_DEPTH = 256
MAX_TEXT_CHARS = 32 * 1024 * 1024

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                       "param", "source", "track", "wbr"})
SKIPPED_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})
_SPACE_RE = re.compile(r"\s+")
REPLACEMENT_CHARACTER = chr(0xFFFD)


def clean(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip()


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent

    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        """Every element below (and including) this one, in document order."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in reversed(node.children) if isinstance(child, Node))

    def chunks(self) -> list:
        """Visible text pieces in document order, whitespace-normalised."""
        out, stack = [], [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                text = clean(item)
                if text:
                    out.append(text)
            elif item.tag not in SKIPPED_TEXT_TAGS:
                stack.extend(reversed(item.children))
        return out

    def text(self) -> str:
        return " ".join(self.chunks())

    def links(self) -> list:
        return [(node.attrs.get("href") or "", node.text()) for node in self.iter()
                if node.tag == "a" and node.attrs.get("href")]

    def media_sources(self) -> list:
        out = []
        for node in self.iter():
            if node.tag in ("img", "video", "source", "audio") and node.attrs.get("src"):
                out.append(node.attrs["src"])
        return list(dict.fromkeys(out))

    def rows(self) -> list:
        """Text pieces of every table row (<tr>) below this element."""
        return [node.chunks() for node in self.iter() if node.tag == "tr"]


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack = [self.root]
        self.nodes = 0
        self.text_chars = 0
        self.truncated = False

    def _new(self, tag, attrs) -> Node | None:
        if self.nodes >= MAX_NODES or len(self.stack) > MAX_DEPTH:
            self.truncated = True
            return None
        self.nodes += 1
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._new(tag, attrs)
        if node is not None and tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._new(tag, attrs)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                del self.stack[position:]
                return
        # A stray closing tag: ignored.

    def handle_data(self, data):
        if self.text_chars + len(data) > MAX_TEXT_CHARS:
            self.truncated = True
            return
        self.text_chars += len(data)
        self.stack[-1].children.append(data)


def parse_html(data: bytes) -> tuple:
    """(root Node, list of warnings). Never raises for bad markup."""
    warnings = []
    text = data.decode("utf-8", errors="replace")
    if REPLACEMENT_CHARACTER in text:
        warnings.append("the HTML file is not valid UTF-8; unreadable characters were replaced")
    builder = _Builder()
    try:
        builder.feed(text)
        builder.close()
    except Exception as exc:  # noqa: BLE001 - html.parser can fail on pathological input
        warnings.append(f"the HTML could not be fully read ({type(exc).__name__}); the part read was used")
    if builder.truncated:
        warnings.append("the HTML file is too large or too deeply nested; only part of it was read")
    return builder.root, warnings
