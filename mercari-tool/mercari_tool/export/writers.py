"""ドラフト・売上データの書き出し。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence

from ..models import CONDITIONS, ListingDraft, Sale


def write_listing_text(draft: ListingDraft, path: str | Path, shipping_label: str = "") -> Path:
    """出品画面にそのまま貼れるテキストを書き出す。

    項目ごとに区切っておき、「タイトル欄にはここ」「説明欄にはここ」と
    迷わずコピーできる形にする。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    hashtags = " ".join(f"#{tag}" for tag in draft.hashtags)
    body = draft.description
    if hashtags:
        body = f"{body}\n\n{hashtags}" if body else hashtags

    sections = [
        "=" * 56,
        f" 出品ドラフト  SKU: {draft.sku}",
        f" 作成: {draft.created_at}",
        "=" * 56,
        "",
        "───────────── ① タイトル（40文字以内）─────────────",
        draft.title,
        f"（{len(draft.title)}文字）",
        "",
        "───────────── ② 価格 ─────────────",
        f"{draft.price:,} 円",
        "",
        "───────────── ③ カテゴリ・状態・配送 ─────────────",
        f"カテゴリ  : {draft.category or '(未設定)'}",
        f"ブランド  : {draft.brand or '(なし)'}",
        f"商品の状態: {CONDITIONS.get(draft.condition, draft.condition)}",
        f"配送方法  : {shipping_label or draft.shipping_method}",
        f"送料負担  : {'出品者（送料込み）' if draft.shipping_payer == 'seller' else '購入者（着払い）'}",
        "",
        "───────────── ④ 商品の説明 ─────────────",
        body,
        "",
    ]

    if draft.title_alternatives:
        sections += [
            "───────────── タイトル候補（差し替え用）─────────────",
            *[f"  - {t}（{len(t)}文字）" for t in draft.title_alternatives],
            "",
        ]

    if draft.image_paths:
        sections += [
            "───────────── 画像 ─────────────",
            f"サムネイル(1枚目推奨): {draft.thumbnail_path or '(なし)'}",
            *[f"  {i}. {p}" for i, p in enumerate(draft.image_paths, start=1)],
            "",
        ]

    sections += [
        "─" * 56,
        "※ 出品操作はメルカリのアプリ／サイトで行ってください。",
        "   自動出品は利用規約で禁止されているため、本ツールでは行いません。",
    ]

    path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return path


def write_draft_json(draft_result, path: str | Path) -> Path:
    """ドラフト一式を JSON で保存する（再利用・監査用）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(draft_result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def export_drafts_csv(drafts: Sequence[ListingDraft], path: str | Path) -> Path:
    """複数ドラフトを1つの CSV にまとめる。

    表計算で一覧を確認したり、出品作業の進捗管理に使う。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "sku", "title", "price", "condition", "category", "brand",
        "shipping_method", "hashtags", "thumbnail", "description",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for draft in drafts:
            writer.writerow(
                [
                    draft.sku,
                    draft.title,
                    draft.price,
                    CONDITIONS.get(draft.condition, draft.condition),
                    draft.category,
                    draft.brand,
                    draft.shipping_method,
                    " ".join(f"#{t}" for t in draft.hashtags),
                    draft.thumbnail_path,
                    draft.description,
                ]
            )
    return path


def export_sales_csv(sales: Sequence[Sale], path: str | Path) -> Path:
    """売上明細を CSV に書き出す（会計ソフト取り込み等に）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "売却日", "出品日", "日数", "SKU", "商品名", "カテゴリ", "仕入先",
        "販売価格", "仕入値", "販売手数料", "送料", "梱包費", "その他",
        "費用合計", "純利益", "利益率", "メモ",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for sale in sales:
            writer.writerow(
                [
                    sale.sold_at.isoformat() if sale.sold_at else "",
                    sale.listed_at.isoformat() if sale.listed_at else "",
                    sale.days_to_sell if sale.days_to_sell is not None else "",
                    sale.sku,
                    sale.product_name,
                    sale.category,
                    sale.supplier_id,
                    sale.sale_price,
                    sale.cost_price,
                    sale.commission,
                    sale.shipping_cost,
                    sale.packaging_cost,
                    sale.other_cost,
                    sale.total_cost,
                    sale.net_profit,
                    f"{sale.margin_rate:.1%}",
                    sale.memo,
                ]
            )
    return path
