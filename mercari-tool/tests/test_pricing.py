"""価格エンジンのテスト。金額計算はここが間違うと全部間違うので厚めに。"""

from __future__ import annotations

import pytest

from mercari_tool.models import MarketStats, Product
from mercari_tool.pricing import MIN_LISTING_PRICE, charm_price


# ── charm_price ────────────────────────────────────────────
def test_charm_price_rounds_to_attractive_endings():
    assert charm_price(1500) == 1480
    assert charm_price(3000) == 2980
    assert charm_price(10000) == 9980


def test_charm_price_is_stable_when_applied_twice():
    """既に端数調整済みの価格を入れても値が動かない。"""
    for price in (1480, 2980, 9980, 480):
        assert charm_price(charm_price(price)) == charm_price(price)


def test_charm_price_never_goes_below_minimum():
    assert charm_price(100) == MIN_LISTING_PRICE
    assert charm_price(0) == MIN_LISTING_PRICE
    assert charm_price(320) >= MIN_LISTING_PRICE


# ── breakdown ──────────────────────────────────────────────
def test_breakdown_components_sum_to_net_profit(engine):
    bd = engine.breakdown(3000, cost_price=1000, shipping_method="nekopos")
    assert bd.listing_price == 3000
    assert bd.commission == 300  # 10%
    assert bd.shipping_cost == 210  # ネコポス
    assert bd.cost_price == 1000
    assert bd.net_profit == bd.listing_price - bd.total_cost
    assert bd.total_cost == (
        bd.commission
        + bd.shipping_cost
        + bd.packaging_cost
        + bd.transfer_fee
        + bd.other_cost
        + bd.cost_price
    )


def test_breakdown_margin_and_roi(engine):
    bd = engine.breakdown(5000, cost_price=1000, shipping_method="nekopos")
    assert bd.margin_rate == pytest.approx(bd.net_profit / 5000)
    assert bd.roi == pytest.approx(bd.net_profit / 1000)


def test_breakdown_handles_zero_cost_without_dividing_by_zero(engine):
    bd = engine.breakdown(3000, cost_price=0)
    assert bd.roi == 0.0


def test_shipping_extra_cost_is_added_to_packaging(engine):
    """専用BOXが必要な配送方法では、資材費が梱包費に乗る。"""
    plain = engine.breakdown(3000, shipping_method="nekopos")
    boxed = engine.breakdown(3000, shipping_method="compact")
    assert boxed.packaging_cost == plain.packaging_cost + 70


def test_unknown_shipping_method_raises(engine):
    with pytest.raises(KeyError, match="未知の配送方法"):
        engine.breakdown(3000, shipping_method="teleport")


# ── 逆算 ───────────────────────────────────────────────────
def test_price_for_profit_produces_at_least_that_profit(engine):
    """逆算した価格で売れば、目標利益を下回らない。"""
    for target in (500, 1000, 5000, 20000):
        price = engine.price_for_profit(target, cost_price=1000, shipping_method="nekopos")
        bd = engine.breakdown(price, cost_price=1000, shipping_method="nekopos")
        assert bd.net_profit >= target


def test_price_for_margin_produces_at_least_that_margin(engine):
    for margin in (0.10, 0.20, 0.30, 0.50):
        price = engine.price_for_margin(margin, cost_price=2000, shipping_method="size60")
        bd = engine.breakdown(price, cost_price=2000, shipping_method="size60")
        assert bd.margin_rate >= margin - 1e-6


def test_price_for_margin_rejects_impossible_target(engine):
    """手数料10%＋利益率95%は達成不能なので、黙って変な値を返さず失敗する。"""
    with pytest.raises(ValueError, match="達成できません"):
        engine.price_for_margin(0.95, cost_price=1000)


def test_breakeven_price_yields_non_negative_profit(engine):
    price = engine.breakeven_price(cost_price=1500, shipping_method="size60")
    bd = engine.breakdown(price, cost_price=1500, shipping_method="size60")
    assert bd.net_profit >= 0
    # 1円下げたら赤字になるくらいギリギリであること
    below = engine.breakdown(price - 50, cost_price=1500, shipping_method="size60")
    assert below.net_profit < 0


# ── 提案 ───────────────────────────────────────────────────
def _market(median: int = 5000, sell_through: float = 0.5) -> MarketStats:
    return MarketStats(
        query="テスト",
        sample_size=20,
        sold_count=int(20 * sell_through),
        median_price=median,
        mean_price=median,
        p25_price=int(median * 0.85),
        p75_price=int(median * 1.15),
        min_price=int(median * 0.6),
        max_price=int(median * 1.5),
        sell_through_rate=sell_through,
    )


def test_recommend_orders_strategies_by_price(engine, product):
    rec = engine.recommend(product, _market())
    prices = {o.strategy: o.price for o in rec.options}
    assert prices["quick"] <= prices["balanced"] <= prices["profit"]


def test_recommend_never_proposes_a_loss_making_price(engine):
    """相場が仕入れ値より安くても、赤字価格は提案しない。"""
    product = Product(sku="X", name="高い仕入れ", cost_price=8000, shipping_method="size60")
    rec = engine.recommend(product, _market(median=3000))
    for option in rec.options:
        assert option.breakdown.net_profit >= 0, option.strategy
    assert any("利益" in note for note in rec.notes)


def test_recommend_picks_profit_strategy_in_fast_moving_market(engine, product):
    rec = engine.recommend(product, _market(sell_through=0.8))
    assert rec.recommended.strategy == "profit"


def test_recommend_picks_quick_strategy_in_slow_market(engine, product):
    rec = engine.recommend(product, _market(sell_through=0.15))
    assert rec.recommended.strategy == "quick"


def test_recommend_works_without_market_data(engine, product):
    rec = engine.recommend(product, None)
    assert rec.recommended is not None
    assert rec.recommended.price > 0
    assert any("相場データが無い" in note for note in rec.notes)


def test_recommend_uses_condition_specific_median_when_available(engine, product):
    market = _market(median=5000)
    market.median_by_condition = {"no_scratch": 7000}
    rec = engine.recommend(product, market)
    balanced = next(o for o in rec.options if o.strategy == "balanced")
    # 全体中央値(5000)ではなく、状態別中央値(7000)側に寄る
    assert balanced.price > 6000


# ── 値下げ交渉 ─────────────────────────────────────────────
def test_evaluate_offer_accepts_healthy_price(engine, product):
    result = engine.evaluate_offer(5000, product)
    assert result["verdict"] == "accept"
    assert result["net_profit"] > 0


def test_evaluate_offer_declines_below_breakeven(engine, product):
    result = engine.evaluate_offer(1200, product)
    assert result["verdict"] == "decline"
    assert result["net_profit"] < 0


def test_evaluate_offer_counters_in_the_thin_margin_band(engine, product):
    """黒字だが目標利益率に届かない価格は、断らず逆提案になる。"""
    breakeven = engine.breakeven_price(
        cost_price=product.cost_price, shipping_method=product.shipping_method
    )
    floor = engine.price_for_margin(
        engine.min_margin,
        cost_price=product.cost_price,
        shipping_method=product.shipping_method,
    )
    between = (breakeven + floor) // 2
    assert breakeven < between < floor
    result = engine.evaluate_offer(between, product)
    assert result["verdict"] == "counter"
    assert result["floor_price"] >= breakeven
