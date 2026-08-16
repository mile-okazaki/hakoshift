"""売上・仕入れの台帳。

手数料と送料は、記録時に手数料テーブルから自動で埋める。
毎回電卓を叩かせない（そして計算ミスを持ち込ませない）ためのもの。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..config import Config
from ..models import Product, Purchase, Sale
from ..pricing import FeeTable
from ..storage import JsonCollection


class Ledger:
    """売上・仕入れ・商品マスタ・仕入れ先をまとめて扱う。"""

    def __init__(self, config: Config, fees: FeeTable | None = None) -> None:
        self.config = config
        self.fees = fees or FeeTable.load(config.fees_path)
        config.data_dir.mkdir(parents=True, exist_ok=True)

        self.sales: JsonCollection[Sale] = JsonCollection(
            config.sales_path, Sale.from_dict, key="id"
        )
        self.purchases: JsonCollection[Purchase] = JsonCollection(
            config.purchases_path, Purchase.from_dict, key="id"
        )
        self.products: JsonCollection[Product] = JsonCollection(
            config.products_path, Product.from_dict, key="sku"
        )

    # ── 記録 ──────────────────────────────────────────────
    def record_sale(
        self,
        sku: str,
        sale_price: int,
        *,
        sold_at: date | None = None,
        listed_at: date | None = None,
        cost_price: int | None = None,
        shipping_method: str | None = None,
        shipping_cost: int | None = None,
        packaging_cost: int | None = None,
        other_cost: int = 0,
        quantity: int = 1,
        memo: str = "",
        product_name: str = "",
    ) -> Sale:
        """売れた1件を記録する。

        手数料・送料・振込手数料は、商品マスタと手数料テーブルから自動計算する。
        明示的に渡された値があればそちらを優先する。
        """
        product = self.products.get(sku)
        method = shipping_method or (product.shipping_method if product else "nekopos")
        option = self.fees.shipping(method)

        if cost_price is None:
            cost_price = self._resolve_cost(sku, product)

        sale = Sale(
            sku=sku,
            product_name=product_name or (product.name if product else sku),
            category=product.category if product else "",
            supplier_id=product.supplier_id if product else "",
            sale_price=int(sale_price),
            cost_price=int(cost_price),
            commission=self.fees.commission(int(sale_price)),
            shipping_cost=option.price if shipping_cost is None else int(shipping_cost),
            packaging_cost=(
                self.fees.default_packaging_cost + option.extra_cost
                if packaging_cost is None
                else int(packaging_cost)
            ),
            # 振込手数料はここでは other_cost にまとめて按分ぶんを入れる
            other_cost=int(other_cost) + self.fees.transfer_fee_per_sale(),
            listed_at=listed_at,
            sold_at=sold_at or date.today(),
            quantity=quantity,
            memo=memo,
        )
        self.sales.upsert(sale)

        # 在庫を減らす
        if product and product.stock > 0:
            product.stock = max(product.stock - quantity, 0)
            self.products.upsert(product)
        return sale

    def record_purchase(
        self,
        sku: str,
        unit_cost: int,
        quantity: int = 1,
        *,
        supplier_id: str = "",
        shipping_cost: int = 0,
        other_cost: int = 0,
        purchased_at: date | None = None,
        memo: str = "",
        product_name: str = "",
    ) -> Purchase:
        """仕入れ1件を記録し、商品マスタの在庫と原価を更新する。"""
        product = self.products.get(sku)
        purchase = Purchase(
            sku=sku,
            product_name=product_name or (product.name if product else sku),
            supplier_id=supplier_id or (product.supplier_id if product else ""),
            unit_cost=int(unit_cost),
            quantity=int(quantity),
            shipping_cost=int(shipping_cost),
            other_cost=int(other_cost),
            purchased_at=purchased_at or date.today(),
            memo=memo,
        )
        self.purchases.upsert(purchase)

        if product:
            product.stock += quantity
            # 原価は移動平均ではなく直近の実質原価を採用する。
            # 中古の一点物が中心なので、直近のほうが実態に合う。
            product.cost_price = purchase.unit_cost_landed
            if purchase.supplier_id:
                product.supplier_id = purchase.supplier_id
            self.products.upsert(product)
        return purchase

    def _resolve_cost(self, sku: str, product: Product | None) -> int:
        """記録時に原価が指定されなかった場合の解決順。"""
        if product and product.cost_price > 0:
            return product.cost_price
        # 商品マスタに無ければ、同じ SKU の直近の仕入れから拾う
        matches = [p for p in self.purchases if p.sku == sku]
        if matches:
            matches.sort(key=lambda p: (p.purchased_at or date.min))
            return matches[-1].unit_cost_landed
        return 0

    # ── 参照 ──────────────────────────────────────────────
    def sales_between(
        self, start: date | None = None, end: date | None = None
    ) -> list[Sale]:
        def in_range(sale: Sale) -> bool:
            if sale.sold_at is None:
                return False
            if start and sale.sold_at < start:
                return False
            if end and sale.sold_at > end:
                return False
            return True

        rows = [s for s in self.sales if in_range(s)]
        rows.sort(key=lambda s: s.sold_at or date.min)
        return rows

    def purchases_between(
        self, start: date | None = None, end: date | None = None
    ) -> list[Purchase]:
        def in_range(purchase: Purchase) -> bool:
            if purchase.purchased_at is None:
                return False
            if start and purchase.purchased_at < start:
                return False
            if end and purchase.purchased_at > end:
                return False
            return True

        rows = [p for p in self.purchases if in_range(p)]
        rows.sort(key=lambda p: p.purchased_at or date.min)
        return rows

    def inventory(self) -> list[dict[str, object]]:
        """在庫として残っている商品と、そこに寝ている金額。"""
        rows = []
        for product in self.products:
            if product.stock <= 0:
                continue
            rows.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "stock": product.stock,
                    "unit_cost": product.cost_price,
                    "tied_up": product.cost_price * product.stock,
                    "supplier_id": product.supplier_id,
                }
            )
        rows.sort(key=lambda r: r["tied_up"], reverse=True)  # type: ignore[arg-type,return-value]
        return rows

    def import_sales_csv(self, path: str | Path) -> int:
        """CSV から売上をまとめて取り込む。

        列: sku,sale_price,sold_at,cost_price,shipping_method,memo
        """
        import csv

        count = 0
        with Path(path).open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if not (row.get("sku") and row.get("sale_price")):
                    continue
                self.record_sale(
                    sku=row["sku"].strip(),
                    sale_price=int(float(row["sale_price"])),
                    sold_at=_parse_day(row.get("sold_at")),
                    listed_at=_parse_day(row.get("listed_at")),
                    cost_price=(
                        int(float(row["cost_price"])) if row.get("cost_price") else None
                    ),
                    shipping_method=row.get("shipping_method") or None,
                    memo=row.get("memo", ""),
                )
                count += 1
        return count


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None
