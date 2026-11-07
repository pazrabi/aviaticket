"""Нарезка HTML-сообщений Telegram на части, не ломающая теги и сущности."""
from __future__ import annotations

import html
import re

TG_LIMIT = 4096
# тег целиком, сущность, перевод строки, слово с одним пробелом после, одиночный пробел, любой символ
_TOKEN = re.compile(r"<[^<>]*>|&#?[0-9A-Za-z]+;|\n|[^<&\n ]+ ?| |.", re.S)
_TAG = re.compile(r"<(/?)([A-Za-z][\w-]*)")


def _close(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _open(stack: list[tuple[str, str]]) -> str:
    return "".join(tag for _, tag in stack)


def _apply(stack: list[tuple[str, str]], tok: str) -> None:
    m = _TAG.match(tok)
    if not m or tok.endswith("/>"):
        return
    name = m.group(2).lower()
    if not m.group(1):
        stack.append((name, tok))
        return
    for i in range(len(stack) - 1, -1, -1):
        if stack[i][0] == name:
            del stack[i:]
            return


def _pieces(tok: str, size: int) -> list[str]:
    if len(tok) <= size or tok.startswith("<") or (tok.startswith("&") and tok.endswith(";")):
        return [tok]
    return [tok[i:i + size] for i in range(0, len(tok), size)]


def split_html(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет HTML на части ≤ limit: по строкам, где можно, иначе по словам, в крайнем случае по символам.

    Открытые теги закрываются в конце части и открываются заново в начале следующей,
    теги и сущности (&amp;) не разрываются.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []
    cur = ""
    newline_cut: tuple[int, list[tuple[str, str]]] | None = None  # позиция после \n и стек на ней

    def flush(upto: int, at_stack: list[tuple[str, str]]) -> str:
        nonlocal cur
        head, tail = cur[:upto], cur[upto:]
        if head.strip():
            chunks.append(head + _close(at_stack))
        cur = _open(at_stack) + tail
        return cur

    for raw in _TOKEN.findall(text):
        for tok in _pieces(raw, max(1, limit // 4)):
            after = list(stack)
            _apply(after, tok)
            if len(cur) + len(tok) + len(_close(after)) > limit and cur:
                if newline_cut and newline_cut[0] > limit // 2:
                    flush(*newline_cut)
                newline_cut = None
                if len(cur) + len(tok) + len(_close(after)) > limit:
                    flush(len(cur), stack)
            cur += tok
            stack = after
            if tok == "\n":
                newline_cut = (len(cur), list(stack))
    if cur.strip() and cur != _open(stack):
        chunks.append(cur + _close(stack))
    return chunks


def html_to_plain(text: str) -> str:
    """Запасной вариант, если Telegram не принял разметку: убрать теги и раскодировать сущности."""
    return html.unescape(re.sub(r"<[^<>]*>", "", text))
