"""売上・利益の集計。

純利益は2つの見方で出す。どちらか一方だけだと判断を誤るため。

  純利益（売上原価ベース）
      その月に「売れた商品」の仕入れ値を、売れた月の費用として引く。
      商売として儲かっているかを見るのはこちら。

  現金収支（キャッシュフロー）
      その月に「支払った仕入れ額」を、支払った月の費用として引く。
      手元のお金が増えているかを見るのはこちら。仕入れを増やした月は
      利益が出ていてもマイナスになる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..models import Purchase, Sale
from ..textutil import display_width, row, rule


@dataclass
class PeriodSummary:
    """1日ぶん、または1か月ぶんの集計。"""

    period: str = ""
    sales_count: int = 0
    revenue: int = 0          # 売上（販売価格の合計）
    cogs: int = 0             # 売上原価（売れた商品の仕入れ値）
    commission: int = 0       # 販売手数料
    shipping: int = 0         # 送料
    packaging: int = 0        # 梱包資材費
    other: int = 0           # その他（振込手数料など）
    purchase_amount: int = 0  # その期間に支払った仕入れ額
    purchase_count: int = 0

    @property
    def total_expense(self) -> int:
        """売上に紐づく費用の合計（売上原価ベース）。"""
        return self.cogs + self.commission + self.shipping + self.packaging + self.other

    @property
    def net_profit(self) -> int:
        """純利益（売上原価ベース）。"""
        return self.revenue - self.total_expense

    @property
    def margin_rate(self) -> float:
        return self.net_profit / self.revenue if self.revenue else 0.0

    @property
    def cash_flow(self) -> int:
        """現金収支。売上 - 手数料類 - その期間の仕入れ支払額。"""
        return (
            self.revenue
            - self.commission
            - self.shipping
            - self.packaging
            - self.other
            - self.purchase_amount
        )

    @property
    def avg_sale_price(self) -> int:
        return round(self.revenue / self.sales_count) if self.sales_count else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "sales_count": self.sales_count,
            "revenue": self.revenue,
            "cogs": self.cogs,
            "commission": self.commission,
            "shipping": self.shipping,
            "packaging": self.packaging,
            "other": self.other,
            "total_expense": self.total_expense,
            "net_profit": self.net_profit,
            "margin_rate": round(self.margin_rate, 4),
            "purchase_amount": self.purchase_amount,
            "purchase_count": self.purchase_count,
            "cash_flow": self.cash_flow,
            "avg_sale_price": self.avg_sale_price,
        }


@dataclass
class GroupSummary:
    """カテゴリ別・仕入れ先別などの切り口での集計。"""

    key: str = ""
    label: str = ""
    sales_count: int = 0
    revenue: int = 0
    net_profit: int = 0
    cogs: int = 0
    days_to_sell: list[int] = field(default_factory=list)

    @property
    def margin_rate(self) -> float:
        return self.net_profit / self.revenue if self.revenue else 0.0

    @property
    def roi(self) -> float:
        return self.net_profit / self.cogs if self.cogs else 0.0

    @property
    def avg_days_to_sell(self) -> float | None:
        if not self.days_to_sell:
            return None
        return round(sum(self.days_to_sell) / len(self.days_to_sell), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "sales_count": self.sales_count,
            "revenue": self.revenue,
            "cogs": self.cogs,
            "net_profit": self.net_profit,
            "margin_rate": round(self.margin_rate, 4),
            "roi": round(self.roi, 4),
            "avg_days_to_sell": self.avg_days_to_sell,
        }


# ── 期間集計 ────────────────────────────────────────────────
def _accumulate(summary: PeriodSummary, sale: Sale) -> None:
    summary.sales_count += 1
    summary.revenue += sale.sale_price
    summary.cogs += sale.cost_price
    summary.commission += sale.commission
    summary.shipping += sale.shipping_cost
    summary.packaging += sale.packaging_cost
    summary.other += sale.other_cost


def _summarize(
    sales: Sequence[Sale],
    purchases: Sequence[Purchase],
    sale_key: Callable[[Sale], str | None],
    purchase_key: Callable[[Purchase], str | None],
) -> list[PeriodSummary]:
    buckets: dict[str, PeriodSummary] = {}

    for sale in sales:
        key = sale_key(sale)
        if key is None:
            continue
        summary = buckets.setdefault(key, PeriodSummary(period=key))
        _accumulate(summary, sale)

    for purchase in purchases:
        key = purchase_key(purchase)
        if key is None:
            continue
        summary = buckets.setdefault(key, PeriodSummary(period=key))
        summary.purchase_amount += purchase.total_cost
        summary.purchase_count += 1

    return [buckets[k] for k in sorted(buckets)]


def daily_summary(
    sales: Sequence[Sale], purchases: Sequence[Purchase] = ()
) -> list[PeriodSummary]:
    """日毎の推移。"""
    return _summarize(
        sales,
        purchases,
        lambda s: s.sold_at.isoformat() if s.sold_at else None,
        lambda p: p.purchased_at.isoformat() if p.purchased_at else None,
    )


def monthly_summary(
    sales: Sequence[Sale], purchases: Sequence[Purchase] = ()
) -> list[PeriodSummary]:
    """月毎の推移。"""
    return _summarize(
        sales,
        purchases,
        lambda s: s.sold_at.strftime("%Y-%m") if s.sold_at else None,
        lambda p: p.purchased_at.strftime("%Y-%m") if p.purchased_at else None,
    )


def overall_summary(
    sales: Sequence[Sale], purchases: Sequence[Purchase] = ()
) -> PeriodSummary:
    """全期間の合計。"""
    total = PeriodSummary(period="合計")
    for sale in sales:
        _accumulate(total, sale)
    for purchase in purchases:
        total.purchase_amount += purchase.total_cost
        total.purchase_count += 1
    return total


# ── 切り口別集計 ────────────────────────────────────────────
def group_by(
    sales: Sequence[Sale],
    key: str = "category",
    labels: dict[str, str] | None = None,
) -> list[GroupSummary]:
    """カテゴリ別・仕入れ先別・SKU別などで集計する。

    Args:
        key: Sale の属性名（"category" / "supplier_id" / "sku"）
        labels: キーから表示名への対応表（仕入れ先IDを名前にするなど）
    """
    buckets: dict[str, GroupSummary] = {}
    for sale in sales:
        raw = getattr(sale, key, "") or "(未分類)"
        group = buckets.setdefault(
            raw, GroupSummary(key=raw, label=(labels or {}).get(raw, raw))
        )
        group.sales_count += 1
        group.revenue += sale.sale_price
        group.cogs += sale.cost_price
        group.net_profit += sale.net_profit
        if sale.days_to_sell is not None:
            group.days_to_sell.append(sale.days_to_sell)

    results = list(buckets.values())
    results.sort(key=lambda g: g.net_profit, reverse=True)
    return results


# ── 表示 ────────────────────────────────────────────────────
#: (見出し, 表示幅, 揃え)
_PERIOD_COLUMNS = [
    ("期間", 10, "<"),
    ("件数", 4, ">"),
    ("売上", 10, ">"),
    ("原価", 9, ">"),
    ("手数料", 8, ">"),
    ("送料", 7, ">"),
    ("純利益", 10, ">"),
    ("利益率", 7, ">"),
    ("仕入額", 10, ">"),
    ("現金収支", 10, ">"),
]


def _period_cells(summary: PeriodSummary) -> list[str]:
    return [
        summary.period,
        f"{summary.sales_count:,}",
        f"{summary.revenue:,}",
        f"{summary.cogs:,}",
        f"{summary.commission:,}",
        f"{summary.shipping:,}",
        f"{summary.net_profit:,}",
        f"{summary.margin_rate:.1%}",
        f"{summary.purchase_amount:,}",
        f"{summary.cash_flow:,}",
    ]


def _render(columns, rows_of_cells) -> list[str]:
    widths = [w for _, w, _ in columns]
    aligns = [a for _, _, a in columns]
    header = row([(title, w, "^") for title, w, _ in columns])
    lines = [header, rule(display_width(header))]
    lines += [
        row(list(zip(cells, widths, aligns))) for cells in rows_of_cells
    ]
    return lines


def format_summary_table(rows: Sequence[PeriodSummary], title: str = "") -> str:
    """期間集計を表にする。全角混じりでも桁が揃うようにしている。"""
    if not rows:
        return f"{title}\n  データがありません。"

    lines: list[str] = []
    if title:
        lines.append(title)
    lines += _render(_PERIOD_COLUMNS, [_period_cells(r) for r in rows])

    if len(rows) > 1:
        total = PeriodSummary(period="合計")
        for r in rows:
            total.sales_count += r.sales_count
            total.revenue += r.revenue
            total.cogs += r.cogs
            total.commission += r.commission
            total.shipping += r.shipping
            total.packaging += r.packaging
            total.other += r.other
            total.purchase_amount += r.purchase_amount
            total.purchase_count += r.purchase_count
        lines.append(rule(display_width(lines[-1])))
        lines.append(row(list(zip(
            _period_cells(total),
            [w for _, w, _ in _PERIOD_COLUMNS],
            [a for _, _, a in _PERIOD_COLUMNS],
        ))))
    return "\n".join(lines)


_GROUP_COLUMNS = [
    ("区分", 22, "<"),
    ("件数", 4, ">"),
    ("売上", 10, ">"),
    ("純利益", 10, ">"),
    ("利益率", 7, ">"),
    ("ROI", 7, ">"),
    ("平均日数", 8, ">"),
]


def format_group_table(rows: Sequence[GroupSummary], title: str = "") -> str:
    if not rows:
        return f"{title}\n  データがありません。"
    cells = [
        [
            group.label,
            f"{group.sales_count:,}",
            f"{group.revenue:,}",
            f"{group.net_profit:,}",
            f"{group.margin_rate:.1%}",
            f"{group.roi:.1%}",
            f"{group.avg_days_to_sell}" if group.avg_days_to_sell is not None else "-",
        ]
        for group in rows
    ]
    lines: list[str] = []
    if title:
        lines.append(title)
    lines += _render(_GROUP_COLUMNS, cells)
    return "\n".join(lines)


def trend_note(rows: Sequence[PeriodSummary]) -> list[str]:
    """推移から読み取れることを短く言語化する。"""
    notes: list[str] = []
    if len(rows) < 2:
        return notes

    latest, previous = rows[-1], rows[-2]
    if previous.revenue:
        change = (latest.revenue - previous.revenue) / previous.revenue
        direction = "増加" if change >= 0 else "減少"
        notes.append(
            f"売上は前期比 {abs(change):.1%} {direction}"
            f"（{previous.revenue:,}円 → {latest.revenue:,}円）"
        )
    if previous.net_profit and latest.net_profit:
        change = (latest.net_profit - previous.net_profit) / abs(previous.net_profit)
        direction = "改善" if change >= 0 else "悪化"
        notes.append(f"純利益は前期比 {abs(change):.1%} {direction}")

    if latest.margin_rate < 0.10 and latest.revenue > 0:
        notes.append(
            f"⚠ 直近の利益率が {latest.margin_rate:.1%} と低水準です。"
            "値付けか仕入れ値を見直してください。"
        )
    negative = [r for r in rows if r.net_profit < 0]
    if negative:
        notes.append(f"⚠ 赤字の期間が {len(negative)} 回あります: "
                     + ", ".join(r.period for r in negative[:5]))
    return notes
