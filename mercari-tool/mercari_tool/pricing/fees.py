"""手数料・送料テーブル。

料金は改定されるため、値はコードではなく data/fees.json に置いている。
改定があったら JSON を直すだけで全機能に反映される。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 同梱の既定テーブル
DEFAULT_FEES_PATH = Path(__file__).resolve().parents[2] / "data" / "fees.json"


@dataclass(frozen=True)
class ShippingOption:
    """配送方法ひとつぶんの料金。"""

    key: str
    label: str
    price: int
    size: str = ""
    #: 専用箱など、配送料とは別にかかる資材費
    extra_cost: int = 0

    @property
    def total(self) -> int:
        return self.price + self.extra_cost


class FeeTable:
    """メルカリの販売手数料・振込手数料・送料をまとめて扱う。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.commission_rate: float = float(data.get("commission_rate", 0.10))
        self.transfer_fee: int = int(data.get("transfer_fee", 200))
        #: 振込手数料を何件の売上で按分するか
        self.amortize_over: int = max(int(data.get("amortize_over", 1)), 1)
        self.default_packaging_cost: int = int(data.get("default_packaging_cost", 0))
        self.last_verified: str = str(data.get("last_verified", "unknown"))

        self._shipping: dict[str, ShippingOption] = {}
        for key, row in (data.get("shipping") or {}).items():
            self._shipping[key] = ShippingOption(
                key=key,
                label=str(row.get("label", key)),
                price=int(row.get("price", 0)),
                size=str(row.get("size", "")),
                extra_cost=int(row.get("extra_cost", 0)),
            )

    # ── 読み込み ────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | Path | None = None) -> "FeeTable":
        target = Path(path) if path else DEFAULT_FEES_PATH
        if not target.exists():
            raise FileNotFoundError(
                f"手数料テーブルが見つかりません: {target}\n"
                "data/fees.json を配置するか Config.fees_path で場所を指定してください。"
            )
        return cls(json.loads(target.read_text(encoding="utf-8")))

    # ── 参照 ──────────────────────────────────────────────
    def shipping_options(self) -> list[ShippingOption]:
        return sorted(self._shipping.values(), key=lambda o: (o.total, o.key))

    def shipping(self, key: str) -> ShippingOption:
        """配送方法を引く。未知のキーは送料0として扱わず明示的に失敗させる。"""
        if key not in self._shipping:
            known = ", ".join(sorted(self._shipping))
            raise KeyError(f"未知の配送方法 '{key}'。使えるのは: {known}")
        return self._shipping[key]

    def commission(self, price: int) -> int:
        """販売手数料（円、四捨五入）。"""
        return round(price * self.commission_rate)

    def transfer_fee_per_sale(self) -> int:
        """1件あたりに按分した振込手数料。"""
        return round(self.transfer_fee / self.amortize_over)

    def describe(self) -> str:
        lines = [
            f"販売手数料: {self.commission_rate:.1%}",
            f"振込手数料: {self.transfer_fee}円（{self.amortize_over}件で按分 → 1件あたり {self.transfer_fee_per_sale()}円）",
            f"梱包資材の既定: {self.default_packaging_cost}円",
            f"料金確認日: {self.last_verified}",
            "",
            "配送方法:",
        ]
        for opt in self.shipping_options():
            extra = f" (+資材{opt.extra_cost}円)" if opt.extra_cost else ""
            lines.append(f"  {opt.key:16s} {opt.price:>5}円{extra}  {opt.label} / {opt.size}")
        return "\n".join(lines)
