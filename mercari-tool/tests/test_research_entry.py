"""相場サンプルの手入力のテスト。

「メルカリの検索結果を見ながら打った雑な行」が、そのまま相場データに
なることを保証する。ここが崩れると日々の入力が全部やり直しになる。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mercari_tool.models import SoldComp
from mercari_tool.research import (
    append_comps,
    load_existing,
    parse_line,
    parse_lines,
    parse_prices,
)


# ── 1行の読み取り ───────────────────────────────────────────
def test_reads_a_bare_number():
    parsed = parse_line("9800")
    assert parsed.ok
    assert parsed.comp.price == 9800


def test_reads_a_price_with_yen_and_comma():
    assert parse_line("9,800円").comp.price == 9800


def test_price_with_yen_wins_over_other_numbers():
    """「円」が付いていれば、行内に大きい数値があってもそちらを採らない。"""
    parsed = parse_line("エアマックス 90 27cm 12800円 在庫999")
    assert parsed.comp.price == 12800


def test_largest_number_is_taken_when_no_unit():
    """単位が無い場合は、型番やサイズより大きい数値を価格とみなす。"""
    parsed = parse_line("エアマックス 90 27cm 11500")
    assert parsed.comp.price == 11500


def test_model_numbers_glued_to_letters_are_not_prices():
    """「990v6」の 990 を価格として拾わない。"""
    parsed = parse_line("ニューバランス 990v6 27cm 18000")
    assert parsed.comp.price == 18000


def test_title_keeps_the_product_words():
    parsed = parse_line("ナイキ エアマックス 90 27cm 11500 売切")
    assert "エアマックス" in parsed.comp.title
    assert "11500" not in parsed.comp.title
    assert "売切" not in parsed.comp.title


def test_sold_marker_sets_sold():
    assert parse_line("9800 売り切れ", default_sold=False).comp.sold is True
    assert parse_line("9800 SOLD", default_sold=False).comp.sold is True


def test_active_marker_clears_sold():
    assert parse_line("出品中 16800円").comp.sold is False
    assert parse_line("販売中 16800円").comp.sold is False


def test_kessai_is_not_read_as_sold():
    """「決済」の『済』を売却済みと読み違えない。"""
    assert parse_line("かんたん決済のみ 9800", default_sold=False).comp.sold is False


def test_condition_word_is_extracted():
    assert parse_line("美品 9800").comp.condition == "no_scratch"
    assert parse_line("新品未使用 9800").comp.condition == "new"
    assert parse_line("未使用に近い 9800").comp.condition == "like_new"


def test_longer_condition_word_wins():
    """「新品未使用」を「新品」で切って読まない。"""
    assert parse_line("新品未使用 9800").comp.condition == "new"
    assert "未使用" not in parse_line("新品未使用 9800").comp.title


def test_condition_flag_applies_when_the_line_has_none():
    assert parse_line("9800", default_condition="small_scratch").comp.condition == "small_scratch"


def test_condition_in_the_line_beats_the_flag():
    parsed = parse_line("美品 9800", default_condition="scratched")
    assert parsed.comp.condition == "no_scratch"


def test_date_is_extracted():
    parsed = parse_line("9800 売切 2026-08-01")
    assert parsed.comp.sold_at == date(2026, 8, 1)
    assert "2026" not in parsed.comp.title


def test_date_is_not_kept_for_unsold_items():
    """出品中の行に売却日を付けない。"""
    parsed = parse_line("出品中 9800 2026-08-01")
    assert parsed.comp.sold is False
    assert parsed.comp.sold_at is None


def test_date_is_not_mistaken_for_a_price():
    parsed = parse_line("2026/08/01 9800円")
    assert parsed.comp.price == 9800


def test_line_without_a_price_fails_with_a_reason():
    parsed = parse_line("これは価格のない行")
    assert not parsed.ok
    assert parsed.error


# ── 複数行 ─────────────────────────────────────────────────
def test_blank_and_comment_lines_are_not_errors():
    comps, failed = parse_lines(["9800", "", "# メモ", "  ", "11500"])
    assert len(comps) == 2
    assert failed == []


def test_unreadable_lines_are_reported_but_do_not_stop_the_rest():
    comps, failed = parse_lines(["9800", "読めない行", "11500"])
    assert [c.price for c in comps] == [9800, 11500]
    assert len(failed) == 1
    assert failed[0].raw == "読めない行"


# ── --prices ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "text", ["9800,11500,8900", "9800 11500 8900", "9800、11500、8900", "9,800,11,500"]
)
def test_price_list_separators(text):
    assert len(parse_prices(text)) >= 2


def test_price_list_applies_the_shared_condition_and_title():
    comps = parse_prices("9800,11500", default_condition="new", title="Tシャツ")
    assert all(c.condition == "new" for c in comps)
    assert all(c.title == "Tシャツ" for c in comps)


def test_price_list_ignores_empty_chunks():
    assert len(parse_prices("9800,,11500,")) == 2


# ── 保存と追記 ──────────────────────────────────────────────
@pytest.fixture
def comps_dir(tmp_path: Path) -> Path:
    target = tmp_path / "comps"
    target.mkdir()
    return target


def test_append_creates_the_file(comps_dir):
    result = append_comps(comps_dir, "ナイキ 27cm", parse_prices("9800,11500"))
    assert result.added == 2
    assert result.total == 2
    assert result.path.exists()


def test_append_adds_to_what_is_already_there(comps_dir):
    append_comps(comps_dir, "ナイキ", parse_prices("9800"))
    result = append_comps(comps_dir, "ナイキ", parse_prices("11500"))
    assert result.added == 1
    assert result.total == 2


def test_identical_samples_are_not_stored_twice(comps_dir):
    """同じ出品を2回書き写しても、相場が二重に重くならない。"""
    append_comps(comps_dir, "ナイキ", parse_prices("9800,11500"))
    result = append_comps(comps_dir, "ナイキ", parse_prices("9800,11500,8900"))
    assert result.added == 1
    assert result.skipped == 2
    assert result.total == 3


def test_same_price_with_a_different_title_is_kept(comps_dir):
    """値段が同じでも別の出品なら、別のサンプルとして残す。"""
    append_comps(comps_dir, "ナイキ", [SoldComp(title="白 27cm", price=9800)])
    result = append_comps(comps_dir, "ナイキ", [SoldComp(title="黒 27cm", price=9800)])
    assert result.added == 1
    assert result.total == 2


def test_replace_drops_the_previous_samples(comps_dir):
    append_comps(comps_dir, "ナイキ", parse_prices("9800,11500"))
    result = append_comps(comps_dir, "ナイキ", parse_prices("12000"), replace=True)
    assert result.total == 1


def test_saved_samples_can_be_read_back(comps_dir):
    append_comps(comps_dir, "ナイキ エアマックス", parse_lines(["美品 9800 売切"])[0])
    loaded = load_existing(comps_dir, "ナイキ エアマックス")
    assert len(loaded) == 1
    assert loaded[0].price == 9800
    assert loaded[0].condition == "no_scratch"


def test_loading_a_missing_query_is_empty_not_an_error(comps_dir):
    assert load_existing(comps_dir, "存在しない検索語") == []


def test_the_saved_file_is_what_the_provider_reads(comps_dir):
    """保存した相場が、そのまま research / draft から読めること。"""
    from mercari_tool.research import DirectoryProvider

    append_comps(comps_dir, "ナイキ エアマックス 90", parse_prices("9800,11500,8900"))
    comps = DirectoryProvider(comps_dir).fetch("ナイキ エアマックス 90")
    assert len(comps) == 3


# ── 手置きCSVとの共存 ───────────────────────────────────────
def _put_csv(comps_dir: Path, query: str) -> Path:
    """同じ検索語の手置きCSVを用意する。"""
    from mercari_tool.research import slugify

    target = comps_dir / f"{slugify(query)}.csv"
    target.write_text(
        "商品名,価格,売却\n白 27cm,9800,売却済み\n黒 27cm,11500,売却済み\n",
        encoding="utf-8",
    )
    return target


def test_append_absorbs_a_hand_made_csv(comps_dir):
    """CSVが既にあっても、書き写したぶんが闇に消えない。"""
    _put_csv(comps_dir, "ナイキ 27cm")
    result = append_comps(comps_dir, "ナイキ 27cm", parse_prices("12000"))
    assert result.added == 1
    assert result.total == 3          # CSVの2件 + 追加1件


def test_provider_reads_the_merged_json_when_both_exist(comps_dir):
    """追記後は、リサーチが CSV ではなく統合済み JSON を読む。"""
    from mercari_tool.research import DirectoryProvider

    _put_csv(comps_dir, "ナイキ 27cm")
    append_comps(comps_dir, "ナイキ 27cm", parse_prices("12000"))
    comps = DirectoryProvider(comps_dir).fetch("ナイキ 27cm")
    assert len(comps) == 3
    assert {c.price for c in comps} == {9800, 11500, 12000}


def test_csv_only_directories_still_work(comps_dir):
    from mercari_tool.research import DirectoryProvider

    _put_csv(comps_dir, "ナイキ 27cm")
    comps = DirectoryProvider(comps_dir).fetch("ナイキ 27cm")
    assert len(comps) == 2


def test_absorbed_csv_rows_are_not_duplicated_on_the_next_append(comps_dir):
    _put_csv(comps_dir, "ナイキ 27cm")
    append_comps(comps_dir, "ナイキ 27cm", parse_prices("12000"))
    result = append_comps(comps_dir, "ナイキ 27cm", parse_prices("13000"))
    assert result.total == 4          # 2(CSV) + 12000 + 13000。CSVが再度足されない


# ── レビューで見つかった読み違いの回帰テスト ─────────────────────
def test_fullwidth_comma_price_is_not_split():
    """IMEの全角カンマ「９，８００円」を 800 円と読み違えない。"""
    assert parse_line("９，８００円 売切").comp.price == 9800


def test_fullwidth_digits_are_read():
    assert parse_line("９８００円").comp.price == 9800


@pytest.mark.parametrize(
    "line,expected",
    [
        ("1.2万円 売切", 12000),
        ("1万2000円 売切", 12000),
        ("9万円 売切", 90000),
        ("1.5万 売切", 15000),
        ("2万 売切", 20000),
    ],
)
def test_man_notation_prices(line, expected):
    """日本語で普通に打つ「万」表記が読める。"""
    parsed = parse_line(line)
    assert parsed.ok, parsed.error
    assert parsed.comp.price == expected


def test_price_glued_to_japanese_text_is_read():
    """「ナイキ9800円」のようにスペース無しで打っても読める。"""
    parsed = parse_line("ナイキ9800円 売切")
    assert parsed.comp.price == 9800
    assert "ナイキ" in parsed.comp.title


def test_discount_arrow_takes_the_new_price():
    """「12000円→9800円」は矢印の後ろが今の価格。"""
    assert parse_line("12000円→9800円 売切").comp.price == 9800


def test_list_price_in_parentheses_is_not_taken():
    """「9800円（定価15000円）」は最初の価格が売値。"""
    assert parse_line("9800円（定価15000円）").comp.price == 9800


def test_prices_below_the_mercari_minimum_are_rejected():
    """300円未満に読めた数値は読み違いとして弾く（型番だけの行など）。"""
    parsed = parse_line("エアマックス90 売切")
    assert not parsed.ok
    assert "300" in parsed.error


def test_decimal_size_is_not_a_price():
    """「27.5cm」の整数部 27 を価格として拾わない。"""
    assert not parse_line("27.5cm エアマックス").ok


def test_same_price_on_different_days_are_separate_records(comps_dir):
    """売却日だけ違う実績は別サンプルとして両方残る。"""
    comps, _ = parse_lines(
        ["エアマックス 9800円 売切 8月1日", "エアマックス 9800円 売切 8月10日"]
    )
    result = append_comps(comps_dir, "ナイキ", comps)
    assert result.added == 2
    assert result.skipped == 0


def test_condition_label_and_key_do_not_double_register(comps_dir):
    """CSV の表示名と行入力の内部キーで、同じ実績が二重登録されない。"""
    append_comps(
        comps_dir, "ナイキ",
        [SoldComp(title="白", price=9800, condition="目立った傷や汚れなし")],
    )
    result = append_comps(
        comps_dir, "ナイキ",
        [SoldComp(title="白", price=9800, condition="no_scratch")],
    )
    assert result.added == 0
    assert result.skipped == 1


def test_price_list_survives_thousands_separators():
    """「9,800, 11,500」を 9円・800円…に分裂させない。"""
    assert [c.price for c in parse_prices("9,800, 11,500")] == [9800, 11500]


def test_price_list_survives_fullwidth_commas():
    assert [c.price for c in parse_prices("9800，11500")] == [9800, 11500]


def test_price_list_drops_impossible_small_values():
    """300円未満（メルカリに存在しない値）は区切りの読み違いとして捨てる。"""
    assert [c.price for c in parse_prices("100,9800")] == [9800]
