"""ドメインモデル。すべて JSON にそのまま往復できる dataclass。"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from typing import Any

# ── 商品状態（メルカリの選択肢に対応）────────────────────────────
CONDITIONS: dict[str, str] = {
    "new": "新品、未使用",
    "like_new": "未使用に近い",
    "no_scratch": "目立った傷や汚れなし",
    "small_scratch": "やや傷や汚れあり",
    "scratched": "傷や汚れあり",
    "bad": "全体的に状態が悪い",
}

#: 状態が相場に与える係数。中央値からの補正に使う。
CONDITION_FACTOR: dict[str, float] = {
    "new": 1.15,
    "like_new": 1.06,
    "no_scratch": 1.00,
    "small_scratch": 0.90,
    "scratched": 0.78,
    "bad": 0.62,
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


class _JsonMixin:
    """dataclass を dict と往復させる最小限のヘルパー。"""

    def to_dict(self) -> dict[str, Any]:
        return {k: _to_jsonable(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        # 日付として復元するのは型注釈が date のフィールドだけ。
        # created_at のような文字列フィールドを巻き込まないよう名前では判定しない。
        date_fields = {
            f.name
            for f in fields(cls)  # type: ignore[arg-type]
            if "date" in str(f.type)
        }
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            kwargs[key] = _parse_date(value) if key in date_fields else value
        return cls(**kwargs)  # type: ignore[arg-type]


# ── 商品 ────────────────────────────────────────────────────
@dataclass
class Product(_JsonMixin):
    """出品対象の商品マスタ。"""

    sku: str
    name: str
    category: str = ""
    brand: str = ""
    condition: str = "no_scratch"
    keywords: list[str] = field(default_factory=list)
    #: 送料区分。fees.json の shipping キーに対応（例: "nekopos", "size60"）
    shipping_method: str = "nekopos"
    #: 送料負担。"seller"（送料込み）/ "buyer"（着払い）
    shipping_payer: str = "seller"
    cost_price: int = 0
    supplier_id: str = ""
    stock: int = 1
    #: 商品固有の訴求ポイント（箇条書き）
    selling_points: list[str] = field(default_factory=list)
    #: 傷・汚れなど正直に書くべき点
    defects: list[str] = field(default_factory=list)
    size_note: str = ""
    notes: str = ""

    @property
    def condition_label(self) -> str:
        return CONDITIONS.get(self.condition, self.condition)


# ── 相場データ ───────────────────────────────────────────────
@dataclass
class SoldComp(_JsonMixin):
    """類似商品の販売実績1件（相場サンプル）。"""

    title: str
    price: int
    sold: bool = True
    condition: str = ""
    sold_at: date | None = None
    listed_at: date | None = None
    url: str = ""
    source: str = "manual"
    shipping_included: bool = True

    @property
    def days_to_sell(self) -> int | None:
        if self.sold_at and self.listed_at:
            return max((self.sold_at - self.listed_at).days, 0)
        return None


@dataclass
class MarketStats(_JsonMixin):
    """相場サンプルを集計した結果。"""

    query: str = ""
    sample_size: int = 0
    sold_count: int = 0
    active_count: int = 0
    median_price: int = 0
    mean_price: int = 0
    p25_price: int = 0
    p75_price: int = 0
    min_price: int = 0
    max_price: int = 0
    #: 売れた割合（0.0-1.0）。回転しやすさの指標。
    sell_through_rate: float = 0.0
    #: 売れるまでの平均日数（分かるサンプルのみ）
    avg_days_to_sell: float | None = None
    #: 直近と過去の中央値比較。1.0 超なら値上がり傾向。
    price_trend: float | None = None
    #: 売れた出品タイトルに頻出する語（多い順）
    hot_keywords: list[str] = field(default_factory=list)
    #: 状態別の中央値
    median_by_condition: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return self.sample_size > 0


# ── 価格 ────────────────────────────────────────────────────
@dataclass
class PriceBreakdown(_JsonMixin):
    """1つの販売価格に対する損益の内訳。"""

    listing_price: int = 0
    commission: int = 0
    shipping_cost: int = 0
    packaging_cost: int = 0
    transfer_fee: int = 0
    other_cost: int = 0
    cost_price: int = 0

    @property
    def total_cost(self) -> int:
        return (
            self.commission
            + self.shipping_cost
            + self.packaging_cost
            + self.transfer_fee
            + self.other_cost
            + self.cost_price
        )

    @property
    def net_profit(self) -> int:
        return self.listing_price - self.total_cost

    @property
    def margin_rate(self) -> float:
        """売上に対する純利益の割合。"""
        if self.listing_price <= 0:
            return 0.0
        return self.net_profit / self.listing_price

    @property
    def roi(self) -> float:
        """仕入れ値に対する純利益の割合（投下資本利益率）。"""
        if self.cost_price <= 0:
            return 0.0
        return self.net_profit / self.cost_price

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "total_cost": self.total_cost,
                "net_profit": self.net_profit,
                "margin_rate": round(self.margin_rate, 4),
                "roi": round(self.roi, 4),
            }
        )
        return data


@dataclass
class PriceOption(_JsonMixin):
    """価格戦略ひとつぶんの提案。"""

    strategy: str  # quick / balanced / profit
    label: str
    price: int
    breakdown: PriceBreakdown
    rationale: str = ""
    #: 相場中央値に対する位置（1.0 = 中央値と同じ）
    vs_median: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "price": self.price,
            "rationale": self.rationale,
            "vs_median": round(self.vs_median, 3) if self.vs_median is not None else None,
            "breakdown": self.breakdown.to_dict(),
        }


@dataclass
class PriceRecommendation(_JsonMixin):
    """価格提案一式。"""

    sku: str = ""
    recommended: PriceOption | None = None
    options: list[PriceOption] = field(default_factory=list)
    #: 値下げ交渉で受けられる下限価格
    floor_price: int = 0
    #: 赤字にならない損益分岐価格
    breakeven_price: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "options": [o.to_dict() for o in self.options],
            "floor_price": self.floor_price,
            "breakeven_price": self.breakeven_price,
            "notes": list(self.notes),
        }


# ── 出品ドラフト ─────────────────────────────────────────────
@dataclass
class ListingDraft(_JsonMixin):
    """出品画面にそのまま貼れる状態まで仕上げた下書き。"""

    sku: str = ""
    title: str = ""
    catchphrase: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    price: int = 0
    condition: str = "no_scratch"
    category: str = ""
    brand: str = ""
    shipping_method: str = "nekopos"
    shipping_payer: str = "seller"
    image_paths: list[str] = field(default_factory=list)
    thumbnail_path: str = ""
    #: 生成されたが採用しなかったタイトル候補
    title_alternatives: list[str] = field(default_factory=list)
    price_recommendation: dict[str, Any] = field(default_factory=dict)
    market_stats: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")


# ── 売上・仕入れ ─────────────────────────────────────────────
@dataclass
class Sale(_JsonMixin):
    """売れた1件。純利益はここで確定させる。"""

    id: str = field(default_factory=lambda: _new_id("sale"))
    sku: str = ""
    product_name: str = ""
    category: str = ""
    supplier_id: str = ""
    sale_price: int = 0
    cost_price: int = 0
    commission: int = 0
    shipping_cost: int = 0
    packaging_cost: int = 0
    other_cost: int = 0
    listed_at: date | None = None
    sold_at: date | None = None
    quantity: int = 1
    memo: str = ""

    @property
    def net_profit(self) -> int:
        return (
            self.sale_price
            - self.cost_price
            - self.commission
            - self.shipping_cost
            - self.packaging_cost
            - self.other_cost
        )

    @property
    def total_cost(self) -> int:
        return self.sale_price - self.net_profit

    @property
    def margin_rate(self) -> float:
        return self.net_profit / self.sale_price if self.sale_price else 0.0

    @property
    def days_to_sell(self) -> int | None:
        if self.listed_at and self.sold_at:
            return max((self.sold_at - self.listed_at).days, 0)
        return None

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "net_profit": self.net_profit,
                "margin_rate": round(self.margin_rate, 4),
                "days_to_sell": self.days_to_sell,
            }
        )
        return data


@dataclass
class Purchase(_JsonMixin):
    """仕入れ1件。まだ売れていない在庫の原価を追える。"""

    id: str = field(default_factory=lambda: _new_id("buy"))
    sku: str = ""
    product_name: str = ""
    supplier_id: str = ""
    unit_cost: int = 0
    quantity: int = 1
    shipping_cost: int = 0
    other_cost: int = 0
    purchased_at: date | None = None
    memo: str = ""

    @property
    def total_cost(self) -> int:
        return self.unit_cost * self.quantity + self.shipping_cost + self.other_cost

    @property
    def unit_cost_landed(self) -> int:
        """送料等を按分した1個あたり実質原価。"""
        if self.quantity <= 0:
            return self.unit_cost
        return round(self.total_cost / self.quantity)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {"total_cost": self.total_cost, "unit_cost_landed": self.unit_cost_landed}
        )
        return data


# ── 仕入れ先 ────────────────────────────────────────────────
@dataclass
class Supplier(_JsonMixin):
    """仕入れ先。スコアリングの入力になる。"""

    id: str = field(default_factory=lambda: _new_id("sup"))
    name: str = ""
    #: 例: "ネット卸", "リサイクルショップ", "フリマ", "問屋", "海外EC"
    channel: str = ""
    url: str = ""
    #: 発注から到着までの日数
    lead_time_days: int = 7
    #: 最低ロット数
    min_order_qty: int = 1
    #: 1個あたり平均仕入れ値
    avg_unit_cost: int = 0
    #: 1回の発注でかかる送料
    order_shipping_cost: int = 0
    #: 不良・不一致の発生率（0.0-1.0）
    defect_rate: float = 0.0
    #: 過去実績の評価（0-5）。取引回数が少ないうちは 3 を既定にする。
    reliability: float = 3.0
    #: 実際に取引した回数
    order_count: int = 0
    notes: str = ""
