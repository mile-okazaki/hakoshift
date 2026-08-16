"""相場リサーチ（トークナイズ・集計・プロバイダ）のテスト。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mercari_tool.models import SoldComp
from mercari_tool.research import analyze, tokenize_ja
from mercari_tool.research.providers import (
    CsvFileProvider,
    DirectoryProvider,
    JsonFileProvider,
    ManualPriceProvider,
    save_comps,
    slugify,
)


# ── トークナイズ ────────────────────────────────────────────
def test_tokenize_extracts_katakana_alnum_and_kanji():
    tokens = tokenize_ja("ナイキ エアマックス 90 レザー 27.5cm")
    assert "ナイキ" in tokens
    assert "エアマックス" in tokens
    assert any(t.startswith("27.5") for t in tokens)


def test_tokenize_drops_generic_selling_words():
    """「送料無料」「美品」は商品を特定しないので拾わない。"""
    tokens = tokenize_ja("送料無料 美品 ナイキ スニーカー")
    assert "送料無料" not in tokens
    assert "美品" not in tokens
    assert "ナイキ" in tokens


def test_tokenize_handles_empty_input():
    assert tokenize_ja("") == []
    assert tokenize_ja(None) == []  # type: ignore[arg-type]


# ── 集計 ───────────────────────────────────────────────────
def _comps(prices, sold=True, **kwargs):
    return [SoldComp(title="テスト商品", price=p, sold=sold, **kwargs) for p in prices]


def test_analyze_computes_basic_statistics():
    stats = analyze(_comps([1000, 2000, 3000, 4000, 5000]), query="テスト")
    assert stats.sample_size == 5
    assert stats.sold_count == 5
    assert stats.median_price == 3000
    assert stats.min_price == 1000
    assert stats.max_price == 5000
    assert stats.sell_through_rate == 1.0


def test_analyze_returns_empty_stats_for_no_samples():
    stats = analyze([], query="無し")
    assert not stats.has_data
    assert stats.median_price == 0
    assert any("0件" in w for w in stats.warnings)


def test_analyze_trims_outliers():
    """桁を間違えた出品が中央値を壊さない。"""
    prices = [3000, 3100, 3200, 3050, 2950, 3150, 999999]
    stats = analyze(_comps(prices))
    assert stats.max_price < 10000
    assert any("外れ値" in w for w in stats.warnings)


def test_analyze_uses_only_sold_items_for_price_stats():
    """出品中の高値は成約価格の統計に混ぜない。"""
    sold = _comps([3000, 3000, 3000], sold=True)
    active = _comps([50000, 50000], sold=False)
    stats = analyze(sold + active)
    assert stats.median_price == 3000
    assert stats.sold_count == 3
    assert stats.active_count == 2
    assert stats.sell_through_rate == pytest.approx(3 / 5)


def test_analyze_warns_when_only_active_listings_available():
    stats = analyze(_comps([5000, 6000], sold=False))
    assert any("売却済みサンプルが0件" in w for w in stats.warnings)


def test_analyze_computes_days_to_sell():
    listed = date(2026, 1, 1)
    comps = [
        SoldComp(title="a", price=1000, sold=True, listed_at=listed, sold_at=listed + timedelta(days=10)),
        SoldComp(title="b", price=1000, sold=True, listed_at=listed, sold_at=listed + timedelta(days=20)),
    ]
    stats = analyze(comps)
    assert stats.avg_days_to_sell == 15.0


def test_analyze_detects_upward_price_trend():
    base = date(2026, 1, 1)
    comps = [
        SoldComp(title="x", price=price, sold=True, sold_at=base + timedelta(days=i * 5))
        for i, price in enumerate([1000, 1000, 1000, 2000, 2000, 2000])
    ]
    stats = analyze(comps)
    assert stats.price_trend is not None
    assert stats.price_trend > 1.5
    assert any("上昇傾向" in w for w in stats.warnings)


def test_analyze_groups_median_by_condition():
    comps = [
        SoldComp(title="a", price=5000, sold=True, condition="新品、未使用"),
        SoldComp(title="b", price=5200, sold=True, condition="新品、未使用"),
        SoldComp(title="c", price=3000, sold=True, condition="目立った傷や汚れなし"),
        SoldComp(title="d", price=3100, sold=True, condition="目立った傷や汚れなし"),
    ]
    stats = analyze(comps)
    assert stats.median_by_condition["new"] == 5100
    assert stats.median_by_condition["no_scratch"] == 3050


def test_analyze_ignores_condition_seen_only_once():
    """1件しかない状態は中央値として採用しない。"""
    comps = [
        SoldComp(title="a", price=5000, sold=True, condition="新品、未使用"),
        SoldComp(title="b", price=3000, sold=True, condition="目立った傷や汚れなし"),
        SoldComp(title="c", price=3100, sold=True, condition="目立った傷や汚れなし"),
    ]
    stats = analyze(comps)
    assert "new" not in stats.median_by_condition
    assert "no_scratch" in stats.median_by_condition


def test_analyze_extracts_hot_keywords():
    comps = [SoldComp(title="ナイキ エアマックス スニーカー", price=8000, sold=True) for _ in range(10)]
    stats = analyze(comps)
    assert "ナイキ" in stats.hot_keywords


# ── プロバイダ ─────────────────────────────────────────────
def test_manual_provider_returns_given_prices():
    comps = ManualPriceProvider([1000, 2000]).fetch("x")
    assert [c.price for c in comps] == [1000, 2000]


def test_csv_provider_reads_english_headers(tmp_path):
    path = tmp_path / "comps.csv"
    path.write_text(
        "title,price,sold,condition,sold_at\n"
        "商品A,3000,1,目立った傷や汚れなし,2026-01-05\n"
        "商品B,3500,0,,\n",
        encoding="utf-8",
    )
    comps = CsvFileProvider(path).fetch("x")
    assert len(comps) == 2
    assert comps[0].price == 3000
    assert comps[0].sold is True
    assert comps[0].sold_at == date(2026, 1, 5)
    assert comps[1].sold is False


def test_csv_provider_reads_japanese_headers(tmp_path):
    """日本語ヘッダーのままエクスポートしたCSVも読める。"""
    path = tmp_path / "comps.csv"
    path.write_text("商品名,価格,売却\n商品A,\"4,500\",売却済み\n", encoding="utf-8")
    comps = CsvFileProvider(path).fetch("x")
    assert comps[0].price == 4500
    assert comps[0].sold is True


def test_csv_provider_skips_rows_without_price(tmp_path):
    path = tmp_path / "comps.csv"
    path.write_text("title,price\n有効,1000\n無効,\n壊れ,abc\n", encoding="utf-8")
    assert len(CsvFileProvider(path).fetch("x")) == 1


def test_json_provider_roundtrips_saved_comps(tmp_path):
    comps = _comps([1000, 2000])
    path = save_comps(tmp_path, "ナイキ スニーカー", comps)
    loaded = JsonFileProvider(path).fetch("x")
    assert [c.price for c in loaded] == [1000, 2000]


def test_directory_provider_finds_file_by_query(tmp_path):
    save_comps(tmp_path, "ナイキ スニーカー", _comps([1000]))
    loaded = DirectoryProvider(tmp_path).fetch("ナイキ スニーカー")
    assert len(loaded) == 1


def test_directory_provider_gives_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="相場データがありません"):
        DirectoryProvider(tmp_path).fetch("存在しない商品")


def test_slugify_keeps_japanese_and_strips_separators():
    assert slugify("ナイキ スニーカー") == "ナイキ-スニーカー"
    assert slugify("  ") == "query"


def test_csv_empty_sold_cell_means_still_listed(tmp_path):
    """売却列が空欄の行は「出品中」。売れた扱いにすると相場が高く出てしまう。"""
    path = tmp_path / "comps.csv"
    path.write_text(
        "title,price,sold\n売れた,3000,1\nまだ,9000,\n", encoding="utf-8"
    )
    comps = CsvFileProvider(path).fetch("x")
    assert [c.sold for c in comps] == [True, False]
    stats = analyze(comps)
    assert stats.sold_count == 1
    assert stats.active_count == 1
    assert stats.median_price == 3000  # 出品中の希望価格に引っ張られない


def test_csv_without_sold_column_treats_all_as_sold(tmp_path):
    """売却列そのものが無いCSVは、全行を売却実績とみなす。"""
    path = tmp_path / "comps.csv"
    path.write_text("title,price\nA,3000\nB,4000\n", encoding="utf-8")
    comps = CsvFileProvider(path).fetch("x")
    assert all(c.sold for c in comps)


def test_magnitude_outlier_is_trimmed_even_in_small_samples():
    """桁間違い級の外れ値は、IQR が効かない少サンプルでも落ちる。"""
    from mercari_tool.models import SoldComp
    from mercari_tool.research import analyze

    comps = [SoldComp(title="x", price=p) for p in (9800, 11500, 250000)]
    stats = analyze(comps, query="q")
    assert stats.median_price <= 11500          # 250000 に引きずられない
    assert any("外れ値" in w for w in stats.warnings)


def test_one_yen_pollution_is_trimmed():
    from mercari_tool.models import SoldComp
    from mercari_tool.research import analyze

    comps = [SoldComp(title="x", price=p) for p in (1, 11000, 11500, 12000)]
    stats = analyze(comps, query="q")
    assert stats.mean_price > 10000             # 1円がいれば 8,625 まで下がる
