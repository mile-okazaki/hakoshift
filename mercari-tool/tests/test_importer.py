"""仕入れ表（CSV）からの一括取り込みのテスト。

現場の表は Excel で作られ、列名は日本語で、順番も揃っていない。
そこを吸収できているかを見る。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mercari_tool.importer import (
    normalize_condition,
    normalize_row,
    read_products_csv,
    rows_to_products,
)
from mercari_tool.models import Product


def _write(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "shiire.csv"
    target.write_text(text, encoding="utf-8")
    return target


# ── 列の読み替え ────────────────────────────────────────────
def test_japanese_headers_map_to_fields():
    row = normalize_row(
        {"sku": "A1", "商品名": "エアマックス", "ブランド": "ナイキ", "仕入値": "4,500"}
    )
    assert row == {"sku": "A1", "name": "エアマックス", "brand": "ナイキ", "cost_price": 4500}


def test_unknown_columns_are_ignored():
    """表に置いてある作業メモの列で落ちない。"""
    row = normalize_row({"sku": "A1", "担当者": "田中", "仕入日": "2026-08-01"})
    assert row == {"sku": "A1"}


def test_empty_cells_are_skipped():
    assert normalize_row({"sku": "A1", "ブランド": "  "}) == {"sku": "A1"}


def test_price_accepts_yen_and_commas():
    assert normalize_row({"仕入値": "4,500円"})["cost_price"] == 4500


@pytest.mark.parametrize("cell", ["スニーカー|ホワイト", "スニーカー,ホワイト"])
def test_list_columns_accept_pipe_or_comma(cell):
    assert normalize_row({"キーワード": cell})["keywords"] == ["スニーカー", "ホワイト"]


def test_pipe_wins_when_both_separators_appear():
    """訴求ポイントに読点代わりのカンマが入っていても、区切りは | を優先する。"""
    row = normalize_row({"訴求ポイント": "軽くて、丈夫|箱付き"})
    assert row["selling_points"] == ["軽くて、丈夫", "箱付き"]


# ── 状態 ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("no_scratch", "no_scratch"),
        ("目立った傷や汚れなし", "no_scratch"),
        ("美品", "no_scratch"),
        ("新品、未使用", "new"),
        ("新品", "new"),
        ("ジャンク", "bad"),
    ],
)
def test_condition_labels_are_normalized(text, expected):
    assert normalize_condition(text) == expected


def test_unknown_condition_is_reported_and_the_row_is_skipped():
    products, errors = rows_to_products([{"sku": "A1", "状態": "ぼろぼろ"}])
    assert products == []
    assert any("ぼろぼろ" in message for message in errors)


# ── 行 → 商品 ───────────────────────────────────────────────
def test_builds_a_product():
    products, errors = rows_to_products(
        [{"sku": "A1", "商品名": "エアマックス", "仕入値": "4500", "在庫": "2"}]
    )
    assert errors == []
    assert products[0].sku == "A1"
    assert products[0].cost_price == 4500
    assert products[0].stock == 2


def test_missing_sku_is_an_error():
    _, errors = rows_to_products([{"sku": "", "商品名": "名無し"}])
    assert any("SKU" in message for message in errors)


def test_duplicate_sku_in_one_file_is_an_error():
    products, errors = rows_to_products([{"sku": "A1"}, {"sku": "A1"}])
    assert len(products) == 1
    assert any("重複" in message for message in errors)


def test_missing_name_warns_but_still_registers():
    """商品名なしは事故のもとなので知らせる。ただし取り込みは止めない。"""
    products, errors = rows_to_products([{"sku": "A1", "仕入値": "500"}])
    assert products and products[0].name == "A1"
    assert any("商品名" in message for message in errors)


def test_existing_products_are_updated_not_replaced():
    """CSV に無い列で、既に登録済みの内容を消さない。"""
    existing = {
        "A1": Product(sku="A1", name="エアマックス", keywords=["スニーカー"], cost_price=4500)
    }
    products, _ = rows_to_products([{"sku": "A1", "在庫": "3"}], existing)
    assert products[0].keywords == ["スニーカー"]   # 消えていない
    assert products[0].cost_price == 4500
    assert products[0].stock == 3


def test_blank_rows_are_not_counted_as_errors():
    products, errors = rows_to_products([{"sku": "", "商品名": "", "仕入値": ""}])
    assert products == []
    assert errors == []


# ── ファイル ────────────────────────────────────────────────
def test_reads_a_csv_file(tmp_path):
    path = _write(
        tmp_path,
        "sku,商品名,ブランド,状態,仕入値,在庫,配送方法,キーワード\n"
        "A1,エアマックス 90,ナイキ,美品,4500,1,size80,スニーカー|ホワイト\n"
        "B2,フリース,ユニクロ,新品,1200,3,nekopos,防寒\n",
    )
    products, errors = read_products_csv(path)
    assert errors == []
    assert [p.sku for p in products] == ["A1", "B2"]
    assert products[0].condition == "no_scratch"
    assert products[0].keywords == ["スニーカー", "ホワイト"]
    assert products[1].shipping_method == "nekopos"


def test_bom_prefixed_csv_from_excel_is_readable(tmp_path):
    target = tmp_path / "excel.csv"
    target.write_text("sku,商品名\nA1,テスト\n", encoding="utf-8-sig")
    products, errors = read_products_csv(target)
    assert errors == []
    assert products[0].sku == "A1"


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        read_products_csv("/存在しない/ファイル.csv")


def test_the_shipped_template_imports_cleanly(tmp_path):
    """同梱の見本が、そのまま取り込める形であること。"""
    from mercari_tool.importer import TEMPLATE_CSV

    products, errors = read_products_csv(_write(tmp_path, TEMPLATE_CSV))
    assert errors == []
    assert len(products) == 1
    assert products[0].condition == "no_scratch"
    assert products[0].selling_points == ["定番のホワイト", "箱付き"]


def test_uppercase_sku_header_is_accepted(tmp_path):
    """Excel でありがちな「SKU」（大文字）ヘッダーでも読める。"""
    products, errors = read_products_csv(
        _write(tmp_path, "SKU,商品名\nA1,テスト\n")
    )
    assert errors == []
    assert products[0].sku == "A1"


def test_existing_products_are_not_mutated_in_place():
    """取り込みが既存オブジェクトを直接書き換えない。

    検証で行を弾いたのに台帳キャッシュ側だけ書き換わっていて、
    別の行の保存時に混入する事故（レビューで再現）を防ぐ。
    """
    original = Product(sku="A1", name="旧名", cost_price=1000)
    products, _ = rows_to_products(
        [{"sku": "A1", "商品名": "新名", "仕入値": "9999"}], {"A1": original}
    )
    assert original.name == "旧名"          # 元は無傷
    assert original.cost_price == 1000
    assert products[0].name == "新名"       # 返り値側だけが更新されている
