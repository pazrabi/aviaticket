from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from avia.tg_text import html_to_plain, split_html


class _Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ok = True

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.ok = False
        else:
            self.stack.pop()


def balanced(chunk: str) -> bool:
    p = _Balance()
    p.feed(chunk)
    p.close()
    broken_entity = re.search(r"&(?![A-Za-z]+;|#\d+;)", chunk)
    return p.ok and not p.stack and broken_entity is None


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^<>]*>", "", text))


def test_short_text_is_returned_as_is():
    assert split_html("<b>hi</b>") == ["<b>hi</b>"]


def test_lines_are_packed_and_each_chunk_fits_limit():
    text = "\n".join(f"<b>{i}.</b> 12 345 ₽ · Победа · <i>aviasales</i>" for i in range(400))
    chunks = split_html(text, limit=4096)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert all(balanced(c) for c in chunks)
    assert "".join(plain(c) for c in chunks).replace("\n", "") == plain(text).replace("\n", "")


def test_tag_spanning_lines_is_closed_and_reopened():
    body = "\n".join(f"строка {i} &amp; ещё" for i in range(300))
    text = f'<blockquote expandable>{body}</blockquote>\n<a href="https://x.test/?a=1&amp;b=2">ссылка</a>'
    chunks = split_html(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert all(balanced(c) for c in chunks), chunks[:2]
    assert chunks[1].startswith("<blockquote expandable>")
    assert plain("".join(chunks)).count("&") == plain(text).count("&")


def test_single_long_line_is_split_without_breaking_entities():
    text = "<b>" + ("Москва&nbsp;—&nbsp;Сочи " * 600) + "</b>"
    chunks = split_html(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert all(balanced(c) for c in chunks)


def test_word_longer_than_limit_is_hard_split():
    text = "x" * 9000
    chunks = split_html(text, limit=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


def test_html_to_plain_strips_tags_and_unescapes():
    assert html_to_plain("<b>A &amp; B</b> &lt;3") == "A & B <3"
