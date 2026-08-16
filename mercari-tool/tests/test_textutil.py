"""表示幅ヘルパーのテスト。日本語の表が崩れないことを保証する。"""

from __future__ import annotations

from mercari_tool.textutil import display_width, pad, row, truncate


def test_display_width_counts_full_width_as_two():
    assert display_width("abc") == 3
    assert display_width("あいう") == 6
    assert display_width("ナイキ90") == 8


def test_pad_aligns_mixed_width_strings_to_the_same_column():
    """全角と半角が混ざっても、パディング後の表示幅が揃う。"""
    for text in ("SKU", "商品名", "ナイキ90", "メンズ/靴"):
        assert display_width(pad(text, 14)) == 14
        assert display_width(pad(text, 14, ">")) == 14
        assert display_width(pad(text, 14, "^")) == 14


def test_pad_right_align_puts_spaces_first():
    assert pad("12", 5, ">") == "   12"


def test_truncate_respects_display_width():
    assert display_width(truncate("あいうえおかきくけこ", 8)) <= 8
    assert truncate("abc", 10) == "abc"       # 収まるものは変えない


def test_row_produces_a_consistent_width():
    columns = [("SKU", 12, "<"), ("商品名", 20, "<"), ("価格", 8, ">")]
    header = row([(t, w, "^") for t, w, _ in columns])
    body = row([("NK-1", 12, "<"), ("ナイキ エアマックス", 20, "<"), ("11,180", 8, ">")])
    assert display_width(header) == display_width(body)
