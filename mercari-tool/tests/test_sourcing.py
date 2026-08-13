"""仕入れ先評価のテスト。"""

from __future__ import annotations

import copy

import pytest

from mercari_tool.models import MarketStats
from mercari_tool.sourcing import SourcingCandidate, SupplierEvaluator, max_viable_cost


@pytest.fixture
def evaluator(engine) -> SupplierEvaluator:
    return SupplierEvaluator(engine)


def _market(median=5000, sell_through=0.5, days=None) -> MarketStats:
    return MarketStats(
        query="テスト",
        sample_size=20,
        sold_count=int(20 * sell_through),
        median_price=median,
        sell_through_rate=sell_through,
        avg_days_to_sell=days,
    )


# ── 仕入れ上限の逆算 ────────────────────────────────────────
def test_max_viable_cost_leaves_the_target_margin(engine):
    limit = max_viable_cost(engine, 5000, "nekopos", target_margin=0.30)
    breakdown = engine.breakdown(5000, cost_price=limit, shipping_method="nekopos")
    assert breakdown.margin_rate >= 0.30


def test_max_viable_cost_is_zero_when_the_price_is_too_low(engine):
    assert max_viable_cost(engine, 400, "size160", target_margin=0.30) == 0


def test_higher_target_margin_lowers_the_cost_ceiling(engine):
    loose = max_viable_cost(engine, 5000, "nekopos", 0.10)
    tight = max_viable_cost(engine, 5000, "nekopos", 0.40)
    assert tight < loose


# ── 評価 ───────────────────────────────────────────────────
def test_landed_cost_includes_shipping_and_defects(evaluator, supplier):
    supplier.min_order_qty = 10
    supplier.order_shipping_cost = 1000   # → 1個あたり100円
    supplier.defect_rate = 0.20           # → 良品率80%
    candidate = SourcingCandidate(
        supplier=supplier, unit_cost=1000, expected_sale_price=5000
    )
    result = evaluator.evaluate(candidate)
    # (1000 + 100) / 0.8 = 1375
    assert result.landed_unit_cost == 1375
    assert result.landed_unit_cost > candidate.unit_cost


def test_evaluate_requires_a_sale_price(evaluator, supplier):
    candidate = SourcingCandidate(supplier=supplier, unit_cost=1000)
    with pytest.raises(ValueError, match="想定売値"):
        evaluator.evaluate(candidate)


def test_market_median_supplies_the_sale_price(evaluator, supplier):
    candidate = SourcingCandidate(supplier=supplier, unit_cost=1000)
    result = evaluator.evaluate(candidate, _market(median=6000))
    assert result.expected_sale_price == 6000


def test_breakeven_qty_counts_units_needed_to_recover_the_outlay(evaluator, supplier):
    """資金の回収は利益ではなく入金額で進む。利益で割ると過大な数になる。"""
    supplier.min_order_qty = 10
    supplier.order_shipping_cost = 0
    candidate = SourcingCandidate(
        supplier=supplier, unit_cost=1000, expected_sale_price=5000
    )
    result = evaluator.evaluate(candidate)
    assert result.order_investment == 10_000
    # 5000 − 手数料500 − 送料210 − 梱包30 − 振込20 = 4240 が1個あたりの入金
    assert result.net_revenue_per_unit == 4240
    assert result.breakeven_qty == 3   # ceil(10000 / 4240)
    assert result.breakeven_qty * result.net_revenue_per_unit >= result.order_investment


def test_single_unit_purchase_is_not_flagged_as_unrecoverable(evaluator, supplier):
    """ロット1個の仕入れは、その1個が売れれば回収できる。誤警告を出さない。"""
    supplier.min_order_qty = 1
    supplier.order_shipping_cost = 0
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=5200, expected_sale_price=10000)
    )
    assert result.breakeven_qty == 1
    assert not any("回収" in flag and flag.startswith("⚠") for flag in result.flags)


def test_lot_purchase_reports_the_recovery_point(evaluator, supplier):
    supplier.min_order_qty = 10
    supplier.order_shipping_cost = 0
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=1000, expected_sale_price=5000)
    )
    assert any("売れた時点で発注額を回収" in flag for flag in result.flags)


def test_faster_turnover_raises_the_monthly_estimate(evaluator, supplier):
    fast = copy.deepcopy(supplier)
    fast.lead_time_days = 2
    slow = copy.deepcopy(supplier)
    slow.lead_time_days = 40

    market = _market(days=10)
    fast_result = evaluator.evaluate(
        SourcingCandidate(supplier=fast, unit_cost=1000, expected_sale_price=5000), market
    )
    slow_result = evaluator.evaluate(
        SourcingCandidate(supplier=slow, unit_cost=1000, expected_sale_price=5000), market
    )
    assert fast_result.cycle_days < slow_result.cycle_days
    assert fast_result.monthly_profit_estimate > slow_result.monthly_profit_estimate


# ── スコアリング ────────────────────────────────────────────
def test_score_stays_within_bounds(evaluator, supplier):
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=1000, expected_sale_price=5000)
    )
    assert 0 <= result.score <= 100
    assert sum(result.score_detail.values()) == pytest.approx(result.score)


def test_cheaper_supplier_scores_higher_all_else_equal(evaluator, supplier):
    cheap = copy.deepcopy(supplier)
    pricey = copy.deepcopy(supplier)
    pricey.id = "sup_pricey"

    cheap_result = evaluator.evaluate(
        SourcingCandidate(supplier=cheap, unit_cost=800, expected_sale_price=5000)
    )
    pricey_result = evaluator.evaluate(
        SourcingCandidate(supplier=pricey, unit_cost=2500, expected_sale_price=5000)
    )
    assert cheap_result.score > pricey_result.score


def test_unreliable_supplier_scores_lower(evaluator, supplier):
    good = copy.deepcopy(supplier)
    bad = copy.deepcopy(supplier)
    bad.id = "sup_bad"
    bad.reliability = 1.0
    bad.defect_rate = 0.18
    bad.lead_time_days = 28
    bad.min_order_qty = 20

    good_result = evaluator.evaluate(
        SourcingCandidate(supplier=good, unit_cost=1000, expected_sale_price=5000)
    )
    bad_result = evaluator.evaluate(
        SourcingCandidate(supplier=bad, unit_cost=1000, expected_sale_price=5000)
    )
    assert good_result.score > bad_result.score


def test_new_supplier_cannot_score_full_reliability(evaluator, supplier):
    """取引実績が浅い相手には信頼度の満点を出さない。"""
    veteran = copy.deepcopy(supplier)
    veteran.reliability = 5.0
    veteran.order_count = 50
    rookie = copy.deepcopy(supplier)
    rookie.id = "sup_rookie"
    rookie.reliability = 5.0
    rookie.order_count = 0

    v = evaluator.evaluate(
        SourcingCandidate(supplier=veteran, unit_cost=1000, expected_sale_price=5000)
    )
    r = evaluator.evaluate(
        SourcingCandidate(supplier=rookie, unit_cost=1000, expected_sale_price=5000)
    )
    assert v.score_detail["reliability"] > r.score_detail["reliability"]


# ── 警告 ───────────────────────────────────────────────────
def test_loss_making_candidate_is_flagged(evaluator, supplier):
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=6000, expected_sale_price=5000)
    )
    assert result.net_profit_per_unit < 0
    assert any("赤字" in flag for flag in result.flags)


def test_large_lot_is_flagged(evaluator, supplier):
    supplier.min_order_qty = 50
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=1000, expected_sale_price=5000)
    )
    assert any("最低ロット" in flag for flag in result.flags)


def test_slow_market_is_flagged(evaluator, supplier):
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=1000, expected_sale_price=5000),
        _market(sell_through=0.1),
    )
    assert any("売却率" in flag for flag in result.flags)


def test_new_supplier_is_flagged(evaluator, supplier):
    supplier.order_count = 1
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=1000, expected_sale_price=5000)
    )
    assert any("実績" in flag for flag in result.flags)


# ── ランキング ─────────────────────────────────────────────
def test_rank_orders_by_score(evaluator, supplier):
    candidates = []
    for index, cost in enumerate([2500, 800, 1500]):
        clone = copy.deepcopy(supplier)
        clone.id = f"sup_{index}"
        candidates.append(
            SourcingCandidate(supplier=clone, unit_cost=cost, expected_sale_price=5000)
        )
    results = evaluator.rank(candidates)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0].candidate.unit_cost == 800


def test_result_is_serialisable(evaluator, supplier):
    result = evaluator.evaluate(
        SourcingCandidate(supplier=supplier, unit_cost=1000, expected_sale_price=5000)
    )
    data = result.to_dict()
    assert data["supplier_name"] == "テスト卸"
    assert isinstance(data["score"], float)
