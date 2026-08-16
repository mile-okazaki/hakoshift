"""端末表示用のテキスト整形。

Python の文字列書式（``f"{s:<10}"``）は文字数で揃えるため、
全角文字が混ざる日本語の表では桁がずれる。表示幅（半角=1, 全角=2）で
数えるヘルパーをここに置く。
"""

from __future__ import annotations

import unicodedata

#: 全角として数える East Asian Width の分類
_WIDE = frozenset("WF")


def display_width(text: str) -> int:
    """端末上での表示幅を返す（全角は2、半角は1）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in _WIDE else 1 for ch in text)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """表示幅が width に収まるよう末尾を切る。"""
    if display_width(text) <= width:
        return text
    limit = width - display_width(ellipsis)
    out: list[str] = []
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(text: str, width: int, align: str = "<") -> str:
    """表示幅を基準に左寄せ・右寄せ・中央寄せする。

    Args:
        align: "<" 左寄せ / ">" 右寄せ / "^" 中央寄せ
    """
    text = truncate(text, width)
    space = max(width - display_width(text), 0)
    if align == ">":
        return " " * space + text
    if align == "^":
        left = space // 2
        return " " * left + text + " " * (space - left)
    return text + " " * space


def row(cells: list[tuple[str, int, str]], gap: str = " ") -> str:
    """(値, 幅, 揃え) の並びを1行に整形する。"""
    return gap.join(pad(text, width, align) for text, width, align in cells)


def rule(width: int, char: str = "-") -> str:
    return char * width
