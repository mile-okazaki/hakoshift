"""仕入れ先の評価。

「安いところ」が最良とは限らない。
到着が遅ければ相場が動くし、不良率が高ければ実質原価が上がり、
最低ロットが大きければ在庫リスクを抱える。
そのため利益だけでなく、時間・品質・ロットもまとめてスコアにする。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import MarketStats, Supplier
from ..pricing import PricingEngine


def max_viable_cost(
    engine: PricingEngine,
    expected_sale_price: int,
    shipping_method: str,
    target_margin: float,
    other_cost: int = 0,
) -> int:
    """「この価格で売るなら、仕入れはいくらまでか」を出す。

    仕入れ判断で最初に知りたい数字。想定売値・目標利益率・送料から、
    許容できる最大の仕入れ値を逆算する。
    """
    fees = engine.fees
    option = fees.shipping(shipping_method)
    commission = fees.commission(expected_sale_price)
    fixed = (
        commission
        + option.price
        + option.extra_cost
        + fees.default_packaging_cost
        + fees.transfer_fee_per_sale()
        + other_cost
    )
    required_profit = expected_sale_price * target_margin
    return max(int(expected_sale_price - fixed - required_profit), 0)


@dataclass
class SourcingCandidate:
    """「この仕入れ先から、この商品を、この単価で」という1案。"""

    supplier: Supplier
    product_name: str = ""
    unit_cost: int = 0
    #: この仕入れ先で確保できる数量
    available_qty: int = 1
    #: 想定売値。未指定なら相場中央値を使う。
    expected_sale_price: int = 0
    shipping_method: str = "nekopos"
    note: str = ""

    def __post_init__(self) -> None:
        if self.unit_cost <= 0:
            self.unit_cost = self.supplier.avg_unit_cost


@dataclass
class SourcingResult:
    """1案の評価結果。"""

    candidate: SourcingCandidate
    expected_sale_price: int = 0
    #: 送料・不良率を織り込んだ1個あたりの実質原価
    landed_unit_cost: int = 0
    net_profit_per_unit: int = 0
    margin_rate: float = 0.0
    roi: float = 0.0
    #: 1個売れたときの入金額（販売価格 − 手数料・送料・梱包）
    net_revenue_per_unit: int = 0
    #: 発注1回で寝かせる金額
    order_investment: int = 0
    #: 最低ロットぶんを売り切ったときの利益
    profit_per_order: int = 0
    #: 発注額を回収するのに必要な販売数。
    #: 回収は「利益」ではなく「入金額」で進むので、入金額で割る。
    breakeven_qty: int = 0
    #: 想定の資金回転日数（リードタイム＋販売にかかる日数）
    cycle_days: int = 0
    #: 月あたりに期待できる利益（回転を考慮）
    monthly_profit_estimate: int = 0
    score: float = 0.0
    score_detail: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier_id": self.candidate.supplier.id,
            "supplier_name": self.candidate.supplier.name,
            "channel": self.candidate.supplier.channel,
            "product_name": self.candidate.product_name,
            "unit_cost": self.candidate.unit_cost,
            "landed_unit_cost": self.landed_unit_cost,
            "expected_sale_price": self.expected_sale_price,
            "net_profit_per_unit": self.net_profit_per_unit,
            "net_revenue_per_unit": self.net_revenue_per_unit,
            "margin_rate": round(self.margin_rate, 4),
            "roi": round(self.roi, 4),
            "order_investment": self.order_investment,
            "profit_per_order": self.profit_per_order,
            "breakeven_qty": self.breakeven_qty,
            "cycle_days": self.cycle_days,
            "monthly_profit_estimate": self.monthly_profit_estimate,
            "score": round(self.score, 1),
            "score_detail": {k: round(v, 1) for k, v in self.score_detail.items()},
            "flags": list(self.flags),
        }


class SupplierEvaluator:
    """仕入れ先候補を採点して順位づけする。"""

    #: スコアの配点（合計100）
    WEIGHTS = {
        "roi": 35.0,          # 資金効率。仕入れで最も効く
        "margin": 20.0,       # 1個あたりの利益率
        "lead_time": 15.0,    # 早く届くほど相場変動リスクが小さい
        "reliability": 15.0,  # 過去の取引実績
        "defect": 10.0,       # 不良率
        "lot": 5.0,           # 最低ロットの重さ
    }

    def __init__(self, engine: PricingEngine) -> None:
        self.engine = engine

    # ── 評価 ──────────────────────────────────────────────
    def evaluate(
        self,
        candidate: SourcingCandidate,
        market: MarketStats | None = None,
        avg_days_to_sell: float | None = None,
    ) -> SourcingResult:
        supplier = candidate.supplier

        sale_price = candidate.expected_sale_price
        if sale_price <= 0 and market and market.has_data:
            sale_price = market.median_price
        if sale_price <= 0:
            raise ValueError(
                "想定売値が決まりません。expected_sale_price を指定するか、"
                "相場データ（MarketStats）を渡してください。"
            )

        lot = max(supplier.min_order_qty, 1)
        # 発注送料をロットで按分し、不良率のぶんだけ良品1個あたりの原価を上げる
        shipping_per_unit = supplier.order_shipping_cost / lot
        good_ratio = max(1.0 - supplier.defect_rate, 0.01)
        landed = int(round((candidate.unit_cost + shipping_per_unit) / good_ratio))

        breakdown = self.engine.breakdown(
            sale_price,
            cost_price=landed,
            shipping_method=candidate.shipping_method,
        )
        profit = breakdown.net_profit

        sell_days = avg_days_to_sell
        if sell_days is None and market and market.avg_days_to_sell:
            sell_days = market.avg_days_to_sell
        if sell_days is None:
            # 売却率から大まかに推定する（売れやすいほど短い）
            rate = market.sell_through_rate if market and market.has_data else 0.4
            sell_days = 14.0 if rate >= 0.6 else (30.0 if rate >= 0.3 else 60.0)

        cycle_days = int(supplier.lead_time_days + sell_days)
        order_investment = int(candidate.unit_cost * lot + supplier.order_shipping_cost)
        profit_per_order = int(profit * lot * good_ratio)
        # 1個売れたときに実際に手元へ入る額。原価はもう払い済みなので足し戻す。
        net_revenue = breakdown.net_profit + breakdown.cost_price
        breakeven_qty = (
            math.ceil(order_investment / net_revenue) if net_revenue > 0 else 0
        )
        monthly = int(profit_per_order * (30.0 / cycle_days)) if cycle_days > 0 else 0

        result = SourcingResult(
            candidate=candidate,
            expected_sale_price=sale_price,
            landed_unit_cost=landed,
            net_profit_per_unit=profit,
            net_revenue_per_unit=net_revenue,
            margin_rate=breakdown.margin_rate,
            roi=breakdown.roi,
            order_investment=order_investment,
            profit_per_order=profit_per_order,
            breakeven_qty=breakeven_qty,
            cycle_days=cycle_days,
            monthly_profit_estimate=monthly,
        )
        result.score, result.score_detail = self._score(result)
        result.flags = self._flags(result, market)
        return result

    def rank(
        self,
        candidates: Sequence[SourcingCandidate],
        market: MarketStats | None = None,
    ) -> list[SourcingResult]:
        """スコアの高い順に並べる。"""
        results = [self.evaluate(c, market) for c in candidates]
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ── 採点 ──────────────────────────────────────────────
    def _score(self, result: SourcingResult) -> tuple[float, dict[str, float]]:
        supplier = result.candidate.supplier
        detail: dict[str, float] = {}

        # ROI: 100% で満点。赤字は0点。
        roi_ratio = max(min(result.roi / 1.0, 1.0), 0.0)
        detail["roi"] = self.WEIGHTS["roi"] * roi_ratio

        # 利益率: 40% で満点
        margin_ratio = max(min(result.margin_rate / 0.40, 1.0), 0.0)
        detail["margin"] = self.WEIGHTS["margin"] * margin_ratio

        # リードタイム: 3日以内で満点、30日で0点
        lead = supplier.lead_time_days
        lead_ratio = max(min((30 - lead) / 27.0, 1.0), 0.0)
        detail["lead_time"] = self.WEIGHTS["lead_time"] * lead_ratio

        # 信頼度: 0-5 の評価。取引実績が少ないうちは満点を出さない。
        reliability = min(max(supplier.reliability, 0.0), 5.0) / 5.0
        experience = min(supplier.order_count / 10.0, 1.0)
        # 実績が浅い相手は 0.6 を上限にする
        detail["reliability"] = self.WEIGHTS["reliability"] * reliability * (
            0.6 + 0.4 * experience
        )

        # 不良率: 0% で満点、20% で0点
        defect_ratio = max(min((0.20 - supplier.defect_rate) / 0.20, 1.0), 0.0)
        detail["defect"] = self.WEIGHTS["defect"] * defect_ratio

        # 最低ロット: 1個で満点、20個で0点
        lot_ratio = max(min((20 - supplier.min_order_qty) / 19.0, 1.0), 0.0)
        detail["lot"] = self.WEIGHTS["lot"] * lot_ratio

        return sum(detail.values()), detail

    def _flags(self, result: SourcingResult, market: MarketStats | None) -> list[str]:
        """判断の前に人が見るべき点。"""
        flags: list[str] = []
        supplier = result.candidate.supplier

        if result.net_profit_per_unit <= 0:
            flags.append("❌ 1個あたり赤字です。この単価では仕入れられません。")
        elif result.margin_rate < self.engine.min_margin:
            flags.append(
                f"⚠ 利益率 {result.margin_rate:.1%} が下限 {self.engine.min_margin:.0%} を下回っています。"
            )

        if supplier.min_order_qty > 1 and result.order_investment > 0:
            flags.append(
                f"最低ロット {supplier.min_order_qty} 個。"
                f"1回の発注で {result.order_investment:,} 円を寝かせることになります。"
            )
        lot = max(supplier.min_order_qty, 1)
        if result.breakeven_qty and result.breakeven_qty > lot:
            flags.append(
                f"⚠ 発注額を回収するには {result.breakeven_qty} 個の販売が必要ですが、"
                f"1回の発注は {lot} 個です。1回では資金を回収しきれません。"
            )
        elif lot > 1 and result.breakeven_qty:
            flags.append(
                f"ロット {lot} 個のうち {result.breakeven_qty} 個売れた時点で発注額を回収できます。"
            )
        if supplier.lead_time_days >= 21:
            flags.append(
                f"リードタイムが {supplier.lead_time_days} 日と長く、到着時に相場が下がっている可能性があります。"
            )
        if supplier.defect_rate >= 0.10:
            flags.append(
                f"不良率 {supplier.defect_rate:.0%}。検品の手間と返品リスクを見込んでください。"
            )
        if supplier.order_count < 3:
            flags.append("取引実績が3回未満です。まずは最小ロットで試すことを勧めます。")
        if market and market.has_data and market.sell_through_rate < 0.3:
            flags.append(
                f"この商品の売却率は {market.sell_through_rate:.0%} と低めです。多量の仕入れは避けてください。"
            )
        return flags


def format_results(results: Sequence[SourcingResult]) -> str:
    """順位づけ結果を読みやすいテキストにする。"""
    if not results:
        return "評価対象の仕入れ先がありません。"
    lines = ["■ 仕入れ先ランキング", ""]
    for rank, result in enumerate(results, start=1):
        candidate = result.candidate
        lines += [
            f"{rank}. {candidate.supplier.name}  [スコア {result.score:.0f}/100]",
            f"   チャネル       : {candidate.supplier.channel or '-'}",
            f"   単価 / 実質原価: {candidate.unit_cost:,}円 / {result.landed_unit_cost:,}円"
            f"（送料按分・不良率込み）",
            f"   想定売値       : {result.expected_sale_price:,}円",
            f"   1個あたり利益  : {result.net_profit_per_unit:,}円"
            f"（利益率 {result.margin_rate:.1%} / ROI {result.roi:.1%}）",
            f"   1個あたり入金  : {result.net_revenue_per_unit:,}円（手数料・送料差引後）",
            f"   1回の発注      : {result.order_investment:,}円投下 → 利益 {result.profit_per_order:,}円",
            f"   資金回転       : {result.cycle_days}日サイクル → 月あたり約 {result.monthly_profit_estimate:,}円",
        ]
        for flag in result.flags:
            lines.append(f"   {flag}")
        lines.append("")
    return "\n".join(lines)
