"""Finding links in a post, and deciding what following one means.

Two kinds:
- **web** links: markdown `[text](url)`, `<url>`, or bare `http(s)://…`. These
  leave scootaloo, so opening one asks first (showing the full URL) unless the
  user has trusted that exact host.
- **post** mentions: `#123` names article 123 on the same board. Following
  one stays inside scootaloo.

Anything inside code (fenced blocks or backticks) is ignored: `#123` in a
code sample isn't a reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_FENCE = re.compile(r"```.*?(```|\Z)|~~~.*?(~~~|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*<?(https?://[^\s)>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_ANGLE = re.compile(r"<(https?://[^\s>]+)>")
_BARE = re.compile(r"(?<![(<\w])(https?://[^\s<>()\[\]]+)")
_MENTION = re.compile(r"(?<![\w&/#])#(\d{1,9})\b")
_TRAILING = ".,;:!?'\""


@dataclass(frozen=True)
class Link:
    kind: str  # "web" | "post"
    target: str  # the URL, or the article number as text
    text: str = ""

    @property
    def host(self) -> str:
        return (urlsplit(self.target).hostname or "").lower() if self.kind == "web" else ""

    @property
    def label(self) -> str:
        if self.kind == "post":
            return f"#{self.target}" + (f"  {self.text}" if self.text else "")
        return self.target if not self.text or self.text == self.target else f"{self.text}  →  {self.target}"


def _strip_code(text: str) -> str:
    text = _FENCE.sub(lambda m: " " * len(m.group(0)), text)
    return _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), text)


def find_links(body: str) -> list[Link]:
    """Every link in `body`, in reading order, without duplicates."""
    text = _strip_code(body or "")
    found: list[tuple[int, Link]] = []
    taken: list[tuple[int, int]] = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    for m in _MD_LINK.finditer(text):
        found.append((m.start(), Link("web", m.group(2), m.group(1).strip())))
        taken.append(m.span())
    for m in _ANGLE.finditer(text):
        if free(*m.span()):
            found.append((m.start(), Link("web", m.group(1))))
            taken.append(m.span())
    for m in _BARE.finditer(text):
        if free(*m.span()):
            url = m.group(1).rstrip(_TRAILING)
            found.append((m.start(), Link("web", url)))
            taken.append((m.start(), m.start() + len(url)))
    for m in _MENTION.finditer(text):
        if free(*m.span()):
            found.append((m.start(), Link("post", m.group(1))))

    out, seen = [], set()
    for _, link in sorted(found, key=lambda x: x[0]):
        key = (link.kind, link.target)
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out


def is_safe_web_url(url: str) -> bool:
    """Only http(s) with a host; never javascript:, file:, data: or anything else."""
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)
