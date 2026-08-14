"""APIキー無しでのタイトル・キャッチコピー組み立てのテスト。

Claude が使えない前提でも「投稿の手前まで」が完結することを保証する。
"""

from __future__ import annotations

import pytest

from mercari_tool.content import build_catchphrases, build_titles
from mercari_tool.content.prompts import TITLE_MAX_CHARS
from mercari_tool.models import MarketStats, Product


@pytest.fixture
def sneaker() -> Product:
    return Product(
        sku="NK-AM90-27",
        name="エアマックス 90",
        brand="ナイキ",
        category="メンズ/靴/スニーカー",
        condition="no_scratch",
        size_note="27cm",
        keywords=["スニーカー", "ホワイト"],
        selling_points=["定番のホワイト", "箱付き"],
        defects=["アウトソールに薄い汚れ"],
    )


def _market(hot: list[str]) -> MarketStats:
    return MarketStats(
        query="テスト", sample_size=20, sold_count=16,
        median_price=10000, sell_through_rate=0.8, hot_keywords=hot,
    )


# ── 長さの保証 ─────────────────────────────────────────────
def test_every_title_fits_the_character_limit(sneaker):
    titles, _ = build_titles(sneaker, _market(["ナイキ", "27cm", "エアマックス"]))
    assert titles
    for candidate in titles:
        assert 0 < len(candidate.text) <= TITLE_MAX_CHARS, candidate.text
        assert candidate.is_valid


def test_long_product_name_is_truncated_by_dropping_segments(sneaker):
    """商品名が長くても、語を落として上限に収める。"""
    sneaker.name = "エアマックス 90 エッセンシャル レザー ローカット スニーカー 白 黒"
    titles, _ = build_titles(sneaker)
    for candidate in titles:
        assert len(candidate.text) <= TITLE_MAX_CHARS


def test_produces_several_distinct_variants(sneaker):
    titles, _ = build_titles(sneaker, _market(["27cm"]))
    texts = [t.text for t in titles]
    assert len(texts) >= 3
    assert len(set(texts)) == len(texts)   # 重複しない


def test_each_variant_explains_its_angle(sneaker):
    titles, _ = build_titles(sneaker, _market(["27cm"]))
    assert all(t.reason for t in titles)


# ── 中身の妥当性 ────────────────────────────────────────────
def test_product_name_is_always_kept(sneaker):
    """語を落とすときも、商品名だけは必ず残す。"""
    titles, _ = build_titles(sneaker)
    assert all("エアマックス" in t.text for t in titles)


def test_brand_is_placed_ahead_of_the_name(sneaker):
    titles, _ = build_titles(sneaker)
    top = titles[0].text
    assert top.index("ナイキ") < top.index("エアマックス")


def test_brand_is_not_repeated_when_already_in_the_name():
    """商品名にブランドが入っているなら前置しない。"""
    product = Product(sku="X", name="ナイキ エアマックス 90", brand="ナイキ")
    titles, _ = build_titles(product)
    assert titles[0].text.count("ナイキ") == 1


def test_size_is_not_repeated_when_already_in_the_name():
    product = Product(sku="X", name="エアマックス 90 27cm", size_note="27cm")
    titles, _ = build_titles(product)
    assert all(t.text.count("27cm") <= 1 for t in titles)


def test_condition_word_only_for_good_condition(sneaker):
    """「美品」は状態が伴うときだけ。傷ありの商品には付けない。"""
    good, _ = build_titles(sneaker)
    assert any("美品" in t.text for t in good)

    sneaker.condition = "scratched"
    poor, _ = build_titles(sneaker)
    assert not any("美品" in t.text for t in poor)
    assert not any("新品" in t.text for t in poor)


def test_new_condition_uses_the_matching_word():
    product = Product(sku="X", name="Tシャツ", condition="new")
    titles, _ = build_titles(product)
    assert any("新品未使用" in t.text for t in titles)


# ── 相場キーワードの扱い ────────────────────────────────────
def test_market_keywords_are_used_when_they_apply(sneaker):
    """相場で頻出かつ商品にも当てはまる語は採用する。"""
    titles, _ = build_titles(sneaker, _market(["27cm", "スニーカー"]))
    assert any("スニーカー" in t.text for t in titles)


def test_market_keywords_that_do_not_apply_are_rejected(sneaker):
    """売れているタイトルに多くても、この商品の事実でない語は入れない。"""
    titles, _ = build_titles(sneaker, _market(["ジョーダン", "限定コラボ", "29cm"]))
    for candidate in titles:
        assert "ジョーダン" not in candidate.text
        assert "限定コラボ" not in candidate.text
        assert "29cm" not in candidate.text


def test_generic_selling_words_are_not_added_as_keywords(sneaker):
    sneaker.keywords = ["送料無料", "即決", "ホワイト"]
    titles, _ = build_titles(sneaker)
    for candidate in titles:
        assert "送料無料" not in candidate.text
        assert "即決" not in candidate.text


def test_works_without_market_data(sneaker):
    titles, phrases = build_titles(sneaker, None)
    assert titles
    assert phrases


def test_minimal_product_still_produces_a_title():
    """商品名しか登録されていなくてもタイトルは出る。"""
    titles, phrases = build_titles(Product(sku="X", name="商品名だけ"))
    assert titles
    assert all("商品名だけ" in t.text for t in titles)
    assert phrases


# ── 状態の語と難点の整合 ────────────────────────────────────
def test_new_condition_word_is_dropped_when_a_defect_is_declared():
    """傷を申告しているのにタイトルへ「新品未使用」と書かない。"""
    product = Product(
        sku="X", name="Tシャツ", condition="new", defects=["襟元に小さなシミ"]
    )
    titles, _ = build_titles(product)
    for candidate in titles:
        assert "新品" not in candidate.text
        assert "未使用" not in candidate.text


def test_like_new_condition_word_is_dropped_when_a_defect_is_declared():
    product = Product(
        sku="X", name="バッグ", condition="like_new", defects=["角にスレ"]
    )
    titles, _ = build_titles(product)
    assert not any("未使用" in t.text for t in titles)


def test_bikin_survives_a_single_minor_defect(sneaker):
    """軽微な難点1つなら「美品」は状態欄と整合する。"""
    assert len(sneaker.defects) == 1
    titles, _ = build_titles(sneaker)
    assert any("美品" in t.text for t in titles)


def test_bikin_is_dropped_when_several_defects_are_declared(sneaker):
    """難点が積み上がったら「美品」は言い過ぎなので外す。"""
    sneaker.defects = ["アウトソールに汚れ", "つま先にスレ", "インソールにヘタリ"]
    titles, _ = build_titles(sneaker)
    assert not any("美品" in t.text for t in titles)


# ── キャッチコピー ─────────────────────────────────────────
def test_catchphrases_use_registered_selling_points(sneaker):
    phrases = build_catchphrases(sneaker)
    assert phrases
    assert any("定番のホワイト" in p for p in phrases)
    assert all("目立った傷や汚れなし" in p or sneaker.name in p for p in phrases)


def test_catchphrases_avoid_unfounded_hype(sneaker):
    for phrase in build_catchphrases(sneaker):
        for hype in ("激安", "大人気", "最安値", "必ず", "保証"):
            assert hype not in phrase


def test_catchphrases_are_deduplicated():
    product = Product(sku="X", name="商品", brand="", selling_points=[])
    phrases = build_catchphrases(product)
    assert len(phrases) == len(set(phrases))


# ── 語の重複 ───────────────────────────────────────────────
def test_size_is_not_duplicated_by_market_keywords(sneaker):
    """サイズ欄と相場キーワードの両方に 27cm があっても1回だけ。"""
    titles, _ = build_titles(sneaker, _market(["27cm", "スニーカー"]))
    for candidate in titles:
        assert candidate.text.count("27cm") <= 1, candidate.text


def test_no_token_appears_twice_in_any_title(sneaker):
    sneaker.keywords = ["27cm", "スニーカー", "ナイキ"]
    titles, _ = build_titles(sneaker, _market(["27cm", "ナイキ", "エアマックス"]))
    for candidate in titles:
        tokens = candidate.text.split()
        assert len(tokens) == len(set(tokens)), candidate.text
