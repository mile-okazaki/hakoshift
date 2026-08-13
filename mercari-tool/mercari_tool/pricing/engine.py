"""価格エンジン。

やること:
  1. ある販売価格に対する損益の内訳を出す
  2. 「この利益がほしい」から必要な販売価格を逆算する
  3. 相場と仕入れ値から、早く売る/バランス/利益重視の3案を出す
  4. 値下げ交渉に応じられる下限価格を出す
"""

from __future__ import annotations

import math
from typing import Callable

from ..models import (
    CONDITION_FACTOR,
    MarketStats,
    PriceBreakdown,
    PriceOption,
    PriceRecommendation,
    Product,
)
from .fees import FeeTable

#: メルカリの最低出品価格
MIN_LISTING_PRICE = 300
#: メルカリの最高出品価格
MAX_LISTING_PRICE = 9_999_999


def charm_price(price: int) -> int:
    """買われやすい端数に寄せる（1500 → 1480、3000 → 2980）。

    100円単位に丸めてから20円引く。既に ...80 で終わる価格を入れても
    同じ値が返る（何度かけても変わらない）。
    """
    if price < MIN_LISTING_PRICE:
        return MIN_LISTING_PRICE
    if price < 1000:
        base = int(round(price / 50.0) * 50)
    else:
        base = int(round(price / 100.0) * 100)
    charmed = base - 20
    return max(charmed, MIN_LISTING_PRICE)


def _clamp_price(price: int) -> int:
    return max(MIN_LISTING_PRICE, min(int(price), MAX_LISTING_PRICE))


class PricingEngine:
    """手数料テーブルを使った損益計算と価格提案。"""

    def __init__(
        self,
        fees: FeeTable,
        target_margin: float = 0.30,
        min_margin: float = 0.12,
    ) -> None:
        self.fees = fees
        self.target_margin = target_margin
        self.min_margin = min_margin

    # ── 1. 内訳 ─────────────────────────────────────────────
    def breakdown(
        self,
        price: int,
        cost_price: int = 0,
        shipping_method: str = "nekopos",
        packaging_cost: int | None = None,
        other_cost: int = 0,
        include_transfer_fee: bool = True,
    ) -> PriceBreakdown:
        """販売価格ひとつに対する損益の内訳。"""
        option = self.fees.shipping(shipping_method)
        pack = (
            self.fees.default_packaging_cost if packaging_cost is None else packaging_cost
        )
        return PriceBreakdown(
            listing_price=int(price),
            commission=self.fees.commission(int(price)),
            shipping_cost=option.price,
            packaging_cost=pack + option.extra_cost,
            transfer_fee=self.fees.transfer_fee_per_sale() if include_transfer_fee else 0,
            other_cost=int(other_cost),
            cost_price=int(cost_price),
        )

    def _fixed_costs(
        self,
        cost_price: int,
        shipping_method: str,
        packaging_cost: int | None,
        other_cost: int,
        include_transfer_fee: bool,
    ) -> int:
        """販売価格に依存しない費用の合計（手数料以外すべて）。"""
        option = self.fees.shipping(shipping_method)
        pack = (
            self.fees.default_packaging_cost if packaging_cost is None else packaging_cost
        )
        transfer = self.fees.transfer_fee_per_sale() if include_transfer_fee else 0
        return int(cost_price) + option.price + pack + option.extra_cost + transfer + int(other_cost)

    # ── 2. 逆算 ─────────────────────────────────────────────
    def _refine(
        self,
        price: int,
        satisfied: Callable[[PriceBreakdown], bool],
        kwargs: dict[str, object],
        max_steps: int = 200,
    ) -> int:
        """解析解から出した価格を、実際の内訳で検算して詰める。

        販売手数料は円単位で四捨五入されるため、連続値の式で解いた価格では
        目標をわずかに（数円ぶん）下回ることがある。1円ずつ上げて実際に
        目標を満たす最小の価格を返す。
        """
        for _ in range(max_steps):
            if satisfied(self.breakdown(price, **kwargs)):  # type: ignore[arg-type]
                return price
            price += 1
        return price

    def price_for_profit(
        self,
        target_profit: int,
        cost_price: int = 0,
        shipping_method: str = "nekopos",
        packaging_cost: int | None = None,
        other_cost: int = 0,
        include_transfer_fee: bool = True,
    ) -> int:
        """目標の純利益（円）を得るために必要な販売価格。

        純利益 = 価格 - 価格×手数料率 - 固定費 を価格について解き、
        手数料の丸め誤差ぶんを検算で詰める。
        """
        fixed = self._fixed_costs(
            cost_price, shipping_method, packaging_cost, other_cost, include_transfer_fee
        )
        rate = self.fees.commission_rate
        seed = _clamp_price(math.ceil((target_profit + fixed) / (1.0 - rate)))
        kwargs = {
            "cost_price": cost_price,
            "shipping_method": shipping_method,
            "packaging_cost": packaging_cost,
            "other_cost": other_cost,
            "include_transfer_fee": include_transfer_fee,
        }
        return self._refine(seed, lambda bd: bd.net_profit >= target_profit, kwargs)

    def price_for_margin(
        self,
        margin: float,
        cost_price: int = 0,
        shipping_method: str = "nekopos",
        packaging_cost: int | None = None,
        other_cost: int = 0,
        include_transfer_fee: bool = True,
    ) -> int:
        """目標の利益率（売上比）を満たす販売価格。

        純利益/価格 = margin を価格について解く。
        手数料率 + 目標利益率 >= 1 の場合はどんな価格でも達成できない。
        """
        rate = self.fees.commission_rate
        denominator = 1.0 - rate - margin
        if denominator <= 0:
            raise ValueError(
                f"利益率 {margin:.0%} は手数料 {rate:.0%} と合わせて100%を超えるため達成できません"
            )
        fixed = self._fixed_costs(
            cost_price, shipping_method, packaging_cost, other_cost, include_transfer_fee
        )
        seed = _clamp_price(math.ceil(fixed / denominator))
        kwargs = {
            "cost_price": cost_price,
            "shipping_method": shipping_method,
            "packaging_cost": packaging_cost,
            "other_cost": other_cost,
            "include_transfer_fee": include_transfer_fee,
        }
        return self._refine(seed, lambda bd: bd.margin_rate >= margin, kwargs)

    def breakeven_price(
        self,
        cost_price: int = 0,
        shipping_method: str = "nekopos",
        packaging_cost: int | None = None,
        other_cost: int = 0,
        include_transfer_fee: bool = True,
    ) -> int:
        """これを下回ると赤字になる価格。"""
        return self.price_for_profit(
            0, cost_price, shipping_method, packaging_cost, other_cost, include_transfer_fee
        )

    # ── 3. 提案 ─────────────────────────────────────────────
    def recommend(
        self,
        product: Product,
        market: MarketStats | None = None,
        other_cost: int = 0,
    ) -> PriceRecommendation:
        """相場・状態・仕入れ値から3つの価格案を作る。"""
        kwargs = dict(
            cost_price=product.cost_price,
            shipping_method=product.shipping_method,
            packaging_cost=None,
            other_cost=other_cost,
            include_transfer_fee=True,
        )
        breakeven = self.breakeven_price(**kwargs)  # type: ignore[arg-type]
        floor = self.price_for_margin(self.min_margin, **kwargs)  # type: ignore[arg-type]

        notes: list[str] = []
        anchors: dict[str, int]

        if market and market.has_data:
            # 状態別の中央値が取れていればそれをそのまま使う。
            # 無ければ全体の中央値に状態係数をかけて補正する。
            same_condition = market.median_by_condition.get(product.condition)
            if same_condition:
                base = int(same_condition)
            else:
                factor = CONDITION_FACTOR.get(product.condition, 1.0)
                base = int(market.median_price * factor)
            anchors = {
                "quick": min(int(base * 0.90), market.p25_price or int(base * 0.90)),
                "balanced": base,
                "profit": max(int(base * 1.12), market.p75_price or int(base * 1.12)),
            }
            notes.append(
                f"相場サンプル {market.sample_size} 件（うち売却済み {market.sold_count} 件）を基準にしています。"
            )
            if market.sample_size < 5:
                notes.append("サンプルが5件未満です。価格提案の確度は低いので参考程度に。")
            if market.sell_through_rate and market.sell_through_rate < 0.3:
                notes.append(
                    f"売却率が {market.sell_through_rate:.0%} と低めです。回転重視なら「早く売る」案を推奨。"
                )
        else:
            # 相場が無いときは利益率から組み立てる
            target = self.price_for_margin(self.target_margin, **kwargs)  # type: ignore[arg-type]
            anchors = {
                "quick": max(floor, int(target * 0.88)),
                "balanced": target,
                "profit": int(target * 1.15),
            }
            notes.append(
                "相場データが無いため、目標利益率から逆算した価格です。"
                "`mercari-tool research` で相場を取り込むと精度が上がります。"
            )

        labels = {
            "quick": "早く売る",
            "balanced": "バランス",
            "profit": "利益重視",
        }
        rationales = {
            "quick": "相場の下限寄り。回転を優先し、在庫を寝かせたくないとき。",
            "balanced": "相場の中央値。もっとも成約しやすい価格帯。",
            "profit": "相場の上限寄り。時間がかかっても利益を取りたいとき。",
        }

        options: list[PriceOption] = []
        for strategy in ("quick", "balanced", "profit"):
            raw = anchors[strategy]
            # どの案も赤字にはしない
            price = charm_price(_clamp_price(max(raw, breakeven)))
            if price < breakeven:  # charm_price で下回った場合の押し戻し
                price = charm_price(breakeven + 100)
            bd = self.breakdown(price, **kwargs)  # type: ignore[arg-type]
            vs_median = (
                price / market.median_price
                if market and market.has_data and market.median_price
                else None
            )
            options.append(
                PriceOption(
                    strategy=strategy,
                    label=labels[strategy],
                    price=price,
                    breakdown=bd,
                    rationale=rationales[strategy],
                    vs_median=vs_median,
                )
            )

        recommended = self._pick_recommended(options, market)

        if recommended.breakdown.net_profit <= 0:
            notes.append(
                "⚠ 推奨価格でも利益が出ません。仕入れ値・送料区分を見直すか、出品を見送ってください。"
            )
        elif recommended.breakdown.margin_rate < self.min_margin:
            notes.append(
                f"⚠ 推奨価格の利益率が {recommended.breakdown.margin_rate:.1%} で、"
                f"下限 {self.min_margin:.0%} を下回っています。"
            )

        return PriceRecommendation(
            sku=product.sku,
            recommended=recommended,
            options=options,
            floor_price=charm_price(max(floor, breakeven)),
            breakeven_price=breakeven,
            notes=notes,
        )

    def _pick_recommended(
        self, options: list[PriceOption], market: MarketStats | None
    ) -> PriceOption:
        """3案のうちどれを推すか決める。

        売却率が高い（=売れやすい）市場なら利益を取りにいき、
        低いなら回転を優先する。判断材料が無ければバランス案。
        """
        by_strategy = {o.strategy: o for o in options}
        if market and market.has_data and market.sell_through_rate:
            if market.sell_through_rate >= 0.6:
                return by_strategy["profit"]
            if market.sell_through_rate < 0.3:
                return by_strategy["quick"]
        return by_strategy["balanced"]

    # ── 4. 値下げ交渉 ────────────────────────────────────────
    def evaluate_offer(
        self,
        offered_price: int,
        product: Product,
        other_cost: int = 0,
    ) -> dict[str, object]:
        """提示された値下げ額を受けるべきか判定する。"""
        kwargs = dict(
            cost_price=product.cost_price,
            shipping_method=product.shipping_method,
            packaging_cost=None,
            other_cost=other_cost,
            include_transfer_fee=True,
        )
        bd = self.breakdown(offered_price, **kwargs)  # type: ignore[arg-type]
        floor = self.price_for_margin(self.min_margin, **kwargs)  # type: ignore[arg-type]
        breakeven = self.breakeven_price(**kwargs)  # type: ignore[arg-type]

        if offered_price >= floor:
            verdict = "accept"
            reason = f"利益率 {bd.margin_rate:.1%} を確保できます。"
        elif offered_price > breakeven:
            verdict = "counter"
            reason = (
                f"利益は残りますが利益率 {bd.margin_rate:.1%} が下限 {self.min_margin:.0%} 未満です。"
                f"{charm_price(floor):,}円で逆提案しましょう。"
            )
        else:
            verdict = "decline"
            reason = f"損益分岐 {breakeven:,}円を下回るため赤字になります。"

        return {
            "verdict": verdict,
            "reason": reason,
            "offered_price": offered_price,
            "net_profit": bd.net_profit,
            "margin_rate": round(bd.margin_rate, 4),
            "floor_price": charm_price(max(floor, breakeven)),
            "breakeven_price": breakeven,
            "breakdown": bd.to_dict(),
        }
