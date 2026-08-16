"""CSV からの一括取り込み。

商品を1点ずつ ``product add`` で打つのは、20点30点になると現実的でない。
仕入れ表をそのまま読ませて、商品マスタに流し込めるようにする。

日本語ヘッダーをそのまま受けるのは、仕入れ表が Excel で作られる前提のため。
列の過不足は許容し、足りないものは既定値のままにする。
"""

from __future__ import annotations

import copy
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import CONDITIONS, Product

#: 日本語ヘッダー → Product の属性名
HEADER_ALIASES: dict[str, str] = {
    "sku": "sku",
    "商品コード": "sku",
    "管理番号": "sku",
    "品番": "sku",
    "商品名": "name",
    "名前": "name",
    "タイトル": "name",
    "カテゴリ": "category",
    "カテゴリー": "category",
    "ブランド": "brand",
    "状態": "condition",
    "商品の状態": "condition",
    "仕入値": "cost_price",
    "仕入価格": "cost_price",
    "仕入金額": "cost_price",
    "原価": "cost_price",
    "在庫": "stock",
    "在庫数": "stock",
    "数量": "stock",
    "配送": "shipping_method",
    "配送方法": "shipping_method",
    "送料区分": "shipping_method",
    "サイズ": "size_note",
    "サイズ表記": "size_note",
    "キーワード": "keywords",
    "訴求ポイント": "selling_points",
    "アピール": "selling_points",
    "難点": "defects",
    "傷": "defects",
    "傷汚れ": "defects",
    "仕入れ先": "supplier_id",
    "仕入先": "supplier_id",
    "備考": "notes",
    "メモ": "notes",
}

#: 複数値を持つ列。`|` か `,` で区切る。
LIST_FIELDS = {"keywords", "selling_points", "defects"}
#: 数値として読む列
INT_FIELDS = {"cost_price", "stock"}

#: 表示名で書かれた状態を内部キーに戻す
_CONDITION_LABELS = {label: key for key, label in CONDITIONS.items()}
_CONDITION_LABELS.update(
    {
        "新品": "new",
        "新品未使用": "new",
        "未使用": "new",
        "ほぼ新品": "like_new",
        "美品": "no_scratch",
        "難あり": "bad",
        "ジャンク": "bad",
    }
)


@dataclass
class ImportReport:
    """取り込み結果。取り込めなかった行は理由付きで残す。"""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated)


def _to_int(value: str) -> int:
    digits = "".join(ch for ch in str(value) if ch.isdigit() or ch == "-")
    return int(digits) if digits not in ("", "-") else 0


def _split_list(value: str) -> list[str]:
    separator = "|" if "|" in value else ","
    return [part.strip() for part in value.split(separator) if part.strip()]


def normalize_condition(value: str) -> str:
    """「美品」のような表示名も内部キーに寄せる。"""
    text = (value or "").strip()
    if not text or text in CONDITIONS:
        return text
    return _CONDITION_LABELS.get(text, text)


#: 内部属性名そのままのヘッダー（sku, name, ...）も受け付ける
_FIELD_NAMES = set(HEADER_ALIASES.values())


def _canonical_field(header: str) -> str | None:
    """ヘッダー1つを内部属性名に読み替える。対応が無ければ None。

    「SKU」「Sku」のような大文字混じりも受ける。Excel のヘッダーは
    人が打つので、大小文字で全行拒否になるのは厳しすぎる。
    """
    key = (header or "").strip()
    if key in HEADER_ALIASES:
        return HEADER_ALIASES[key]
    lowered = key.lower()
    if lowered in HEADER_ALIASES:
        return HEADER_ALIASES[lowered]
    if lowered in _FIELD_NAMES:
        return lowered
    return None


def normalize_row(raw: dict[str, str]) -> dict[str, object]:
    """CSV の1行を Product の属性名に揃える。"""
    row: dict[str, object] = {}
    for key, value in raw.items():
        name = _canonical_field(key)
        if name is None:
            continue
        text = (value or "").strip()
        if not text:
            continue
        if name in INT_FIELDS:
            row[name] = _to_int(text)
        elif name in LIST_FIELDS:
            row[name] = _split_list(text)
        elif name == "condition":
            row[name] = normalize_condition(text)
        else:
            row[name] = text
    return row


def rows_to_products(
    rows: Iterable[dict[str, str]],
    existing: dict[str, Product] | None = None,
) -> tuple[list[Product], list[str]]:
    """CSV の行から Product を組み立てる。

    既存の SKU は上書きではなく更新にする。CSV に無い列（過去に登録した
    キーワードなど）を、列が空という理由で消してしまわないため。

    Returns:
        (組み立てた商品, エラーメッセージ)
    """
    existing = existing or {}
    products: list[Product] = []
    errors: list[str] = []
    seen: set[str] = set()

    for index, raw in enumerate(rows, start=2):  # 1行目はヘッダー
        row = normalize_row(raw)
        sku = str(row.get("sku") or "").strip()
        if not sku:
            if row:
                errors.append(f"{index}行目: SKU が空です")
            continue
        if sku in seen:
            errors.append(f"{index}行目: SKU '{sku}' が重複しています")
            continue
        condition = str(row.get("condition") or "")
        if condition and condition not in CONDITIONS:
            errors.append(
                f"{index}行目: 状態 '{condition}' は不明です"
                f"（{'/'.join(CONDITIONS)}）"
            )
            continue

        seen.add(sku)
        base = existing.get(sku)
        if base is None and not row.get("name"):
            # SKU をそのまま商品名にすると検索も相場取得も当たらない
            errors.append(f"{index}行目: '{sku}' に商品名がありません（SKU を仮の名前にしました）")
        # 既存の Product を直接書き換えない。呼び出し元が検証で行を弾いたのに
        # 台帳キャッシュ側だけ書き換わっている、という事故を防ぐ。
        product = (
            copy.deepcopy(base) if base else Product(sku=sku, name=str(row.get("name") or sku))
        )
        for name, value in row.items():
            if name == "sku":
                continue
            setattr(product, name, value)
        products.append(product)

    return products, errors


def read_products_csv(
    path: str | Path,
    existing: dict[str, Product] | None = None,
) -> tuple[list[Product], list[str]]:
    """CSV ファイルを読んで Product の一覧にする。"""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"ファイルが見つかりません: {target}")
    with target.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return [], ["ヘッダー行がありません"]
        return rows_to_products(reader, existing)


#: `product import --template` で書き出す見本
TEMPLATE_CSV = (
    "sku,商品名,ブランド,カテゴリ,状態,サイズ,仕入値,在庫,配送方法,"
    "キーワード,訴求ポイント,難点,備考\n"
    "NK-AM90-27,エアマックス 90,ナイキ,メンズ/靴/スニーカー,目立った傷や汚れなし,"
    "27cm,4500,1,size80,スニーカー|ホワイト,定番のホワイト|箱付き,"
    "アウトソールに薄い汚れ,\n"
)
