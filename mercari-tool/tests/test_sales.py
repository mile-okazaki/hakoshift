"""売上台帳と集計のテスト。"""

from __future__ import annotations

from datetime import date

import pytest

from mercari_tool.models import Product, Purchase, Sale
from mercari_tool.sales import Ledger
from mercari_tool.sales.analytics import (
    daily_summary,
    group_by,
    monthly_summary,
    overall_summary,
    trend_note,
)


# ── 台帳 ───────────────────────────────────────────────────
@pytest.fixture
def ledger(config, product) -> Ledger:
    led = Ledger(config)
    led.products.upsert(product)
    return led


def test_record_sale_fills_fees_automatically(ledger):
    sale = ledger.record_sale("TEST001", 5000, sold_at=date(2026, 3, 1))
    assert sale.commission == 500          # 10%
    assert sale.shipping_cost == 210       # ネコポス
    assert sale.cost_price == 1000         # 商品マスタから
    assert sale.other_cost == 20           # 振込手数料の按分（200/10）
    assert sale.net_profit == 5000 - 500 - 210 - 30 - 20 - 1000


def test_record_sale_decrements_stock(ledger):
    assert ledger.products.get("TEST001").stock == 1
    ledger.record_sale("TEST001", 5000)
    assert ledger.products.get("TEST001").stock == 0


def test_record_sale_never_drives_stock_negative(ledger):
    ledger.record_sale("TEST001", 5000)
    ledger.record_sale("TEST001", 5000)
    assert ledger.products.get("TEST001").stock == 0


def test_explicit_values_override_automatic_ones(ledger):
    sale = ledger.record_sale("TEST001", 5000, cost_price=2500, shipping_cost=0)
    assert sale.cost_price == 2500
    assert sale.shipping_cost == 0


def test_record_purchase_updates_stock_and_landed_cost(ledger):
    purchase = ledger.record_purchase(
        "TEST001", unit_cost=800, quantity=5, shipping_cost=500
    )
    # (800*5 + 500) / 5 = 900
    assert purchase.unit_cost_landed == 900
    product = ledger.products.get("TEST001")
    assert product.stock == 6            # 元の1 + 仕入れ5
    assert product.cost_price == 900     # 実質原価に更新


def test_sale_falls_back_to_latest_purchase_cost(config):
    """商品マスタに原価が無くても、直近の仕入れから拾う。"""
    ledger = Ledger(config)
    ledger.products.upsert(Product(sku="NEW01", name="新商品", cost_price=0))
    ledger.record_purchase("NEW01", unit_cost=1200, quantity=1, purchased_at=date(2026, 1, 1))
    ledger.products.upsert(Product(sku="NEW01", name="新商品", cost_price=0))  # 原価を消す
    sale = ledger.record_sale("NEW01", 4000)
    assert sale.cost_price == 1200


def test_sales_between_filters_by_date(ledger):
    ledger.record_sale("TEST001", 1000, sold_at=date(2026, 1, 15))
    ledger.record_sale("TEST001", 2000, sold_at=date(2026, 2, 15))
    ledger.record_sale("TEST001", 3000, sold_at=date(2026, 3, 15))
    rows = ledger.sales_between(date(2026, 2, 1), date(2026, 2, 28))
    assert [s.sale_price for s in rows] == [2000]


def test_inventory_lists_tied_up_capital(ledger):
    ledger.record_purchase("TEST001", unit_cost=1000, quantity=4)
    inventory = ledger.inventory()
    assert inventory[0]["sku"] == "TEST001"
    assert inventory[0]["stock"] == 5
    assert inventory[0]["tied_up"] == inventory[0]["unit_cost"] * 5


def test_ledger_persists_across_instances(config, product):
    first = Ledger(config)
    first.products.upsert(product)
    first.record_sale("TEST001", 5000, sold_at=date(2026, 3, 1))

    second = Ledger(config)
    assert len(second.sales.all()) == 1
    assert second.sales.all()[0].sale_price == 5000


def test_import_sales_csv(config, product, tmp_path):
    ledger = Ledger(config)
    ledger.products.upsert(product)
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(
        "sku,sale_price,sold_at\nTEST001,3000,2026-04-01\nTEST001,4000,2026-04-02\n",
        encoding="utf-8",
    )
    assert ledger.import_sales_csv(csv_path) == 2
    assert len(ledger.sales.all()) == 2


# ── 集計 ───────────────────────────────────────────────────
def _sale(price: int, cost: int, day: date, **kwargs) -> Sale:
    return Sale(
        sku=kwargs.get("sku", "S1"),
        category=kwargs.get("category", "本"),
        sale_price=price,
        cost_price=cost,
        commission=round(price * 0.1),
        shipping_cost=210,
        packaging_cost=30,
        other_cost=20,
        sold_at=day,
        listed_at=kwargs.get("listed_at"),
    )


def test_daily_summary_buckets_by_day():
    sales = [
        _sale(3000, 1000, date(2026, 5, 1)),
        _sale(4000, 1500, date(2026, 5, 1)),
        _sale(5000, 2000, date(2026, 5, 2)),
    ]
    rows = daily_summary(sales)
    assert [r.period for r in rows] == ["2026-05-01", "2026-05-02"]
    assert rows[0].sales_count == 2
    assert rows[0].revenue == 7000


def test_monthly_summary_buckets_by_month():
    sales = [_sale(3000, 1000, date(2026, 5, 1)), _sale(4000, 1000, date(2026, 6, 1))]
    rows = monthly_summary(sales)
    assert [r.period for r in rows] == ["2026-05", "2026-06"]


def test_net_profit_subtracts_every_cost():
    rows = daily_summary([_sale(10000, 3000, date(2026, 5, 1))])
    row = rows[0]
    assert row.cogs == 3000
    assert row.commission == 1000
    assert row.net_profit == 10000 - 3000 - 1000 - 210 - 30 - 20
    assert row.margin_rate == pytest.approx(row.net_profit / 10000)


def test_cash_flow_differs_from_net_profit_when_stocking_up():
    """仕入れを増やした月は、黒字でも現金収支はマイナスになりうる。"""
    sales = [_sale(10000, 3000, date(2026, 5, 10))]
    purchases = [
        Purchase(sku="S1", unit_cost=3000, quantity=10, purchased_at=date(2026, 5, 20))
    ]
    row = monthly_summary(sales, purchases)[0]
    assert row.net_profit > 0
    assert row.purchase_amount == 30000
    assert row.cash_flow < 0
    assert row.cash_flow == 10000 - 1000 - 210 - 30 - 20 - 30000


def test_purchase_only_period_still_appears():
    """売上ゼロで仕入れだけの月も、集計から消えない。"""
    purchases = [Purchase(sku="S1", unit_cost=5000, quantity=1, purchased_at=date(2026, 7, 3))]
    rows = monthly_summary([], purchases)
    assert len(rows) == 1
    assert rows[0].period == "2026-07"
    assert rows[0].purchase_amount == 5000
    assert rows[0].cash_flow == -5000


def test_overall_summary_totals_everything():
    sales = [_sale(3000, 1000, date(2026, 5, 1)), _sale(4000, 1000, date(2026, 6, 1))]
    total = overall_summary(sales)
    assert total.sales_count == 2
    assert total.revenue == 7000
    assert total.avg_sale_price == 3500


def test_group_by_category_sorts_by_profit():
    sales = [
        _sale(10000, 2000, date(2026, 5, 1), category="家電"),
        _sale(2000, 1500, date(2026, 5, 1), category="本"),
    ]
    groups = group_by(sales, "category")
    assert groups[0].label == "家電"
    assert groups[0].net_profit > groups[1].net_profit


def test_group_by_labels_unknown_key():
    groups = group_by([_sale(1000, 100, date(2026, 5, 1), category="")], "category")
    assert groups[0].label == "(未分類)"


def test_group_by_computes_average_days_to_sell():
    sales = [
        _sale(3000, 1000, date(2026, 5, 11), listed_at=date(2026, 5, 1)),
        _sale(3000, 1000, date(2026, 5, 21), listed_at=date(2026, 5, 1)),
    ]
    groups = group_by(sales, "category")
    assert groups[0].avg_days_to_sell == 15.0


def test_trend_note_flags_declining_profit():
    sales = [
        _sale(10000, 2000, date(2026, 5, 1)),
        _sale(3000, 2500, date(2026, 6, 1)),
    ]
    notes = trend_note(monthly_summary(sales))
    assert any("売上" in n for n in notes)


def test_trend_note_is_empty_for_single_period():
    assert trend_note(monthly_summary([_sale(1000, 100, date(2026, 5, 1))])) == []
