"""コマンドラインインターフェース。

    mercari-tool <グループ> <コマンド> [オプション]

グループ:
    product   商品マスタ
    research  相場リサーチ
    price     価格設定・値下げ判定
    image     画像加工・サムネイル
    draft     出品ドラフト一括生成
    comment   コメント返信文の生成
    supplier  仕入れ先マスタ
    sourcing  仕入れ判断
    sales     売上・仕入れの記録と集計
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Config
from .context import AppContext
from .models import CONDITIONS, Product, Supplier
from .research import analyze, save_comps
from .research.analyzer import format_stats
from .sales.analytics import (
    daily_summary,
    format_group_table,
    format_summary_table,
    group_by,
    monthly_summary,
    overall_summary,
    trend_note,
)
from .sourcing import SourcingCandidate, max_viable_cost
from .sourcing.evaluator import format_results
from .textutil import display_width, row, rule


# ── 表示ヘルパー ────────────────────────────────────────────
def _print(*parts: Any) -> None:
    print(*parts, flush=True)


def _fail(message: str) -> int:
    print(f"エラー: {message}", file=sys.stderr, flush=True)
    return 1


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"日付の形式が不正です: {value}（例: 2026-08-01）")


def _require_product(ctx: AppContext, sku: str) -> Product:
    product = ctx.ledger.products.get(sku)
    if product is None:
        known = ", ".join(p.sku for p in ctx.ledger.products) or "(登録なし)"
        raise SystemExit(
            f"エラー: SKU '{sku}' が見つかりません。\n登録済み: {known}\n"
            f"追加: mercari-tool product add --sku {sku} --name 商品名"
        )
    return product


# ══════════════════════════════════════════════════════════
# init / fees
# ══════════════════════════════════════════════════════════
def cmd_init(ctx: AppContext, args: argparse.Namespace) -> int:
    ctx.config.ensure_dirs()
    _print(f"データディレクトリを用意しました: {ctx.config.data_dir.resolve()}")
    for path in (
        ctx.config.products_path,
        ctx.config.sales_path,
        ctx.config.purchases_path,
        ctx.config.suppliers_path,
    ):
        if not path.exists():
            path.write_text("[]\n", encoding="utf-8")
            _print(f"  作成: {path.name}")

    env_path = Path(".env")
    example = Path(__file__).resolve().parents[1] / ".env.example"
    if not env_path.exists() and example.exists():
        env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        _print("  作成: .env（.env.example からコピー）")
        _print("\n次の手順:")
        _print("  1. .env を開き ANTHROPIC_API_KEY を設定")
        _print("  2. mercari-tool product add --sku SKU001 --name 商品名 --cost 1000")
    return 0


def cmd_fees(ctx: AppContext, args: argparse.Namespace) -> int:
    _print(ctx.fees.describe())
    _print("\n※ 料金は改定されます。data/fees.json を最新の公式情報に合わせてください。")
    return 0


# ══════════════════════════════════════════════════════════
# product
# ══════════════════════════════════════════════════════════
def cmd_product_add(ctx: AppContext, args: argparse.Namespace) -> int:
    existing = ctx.ledger.products.get(args.sku)
    product = existing or Product(sku=args.sku, name=args.name or args.sku)
    if args.name:
        product.name = args.name
    for field_name, value in (
        ("category", args.category),
        ("brand", args.brand),
        ("condition", args.condition),
        ("shipping_method", args.shipping),
        ("size_note", args.size),
        ("notes", args.notes),
        ("supplier_id", args.supplier),
    ):
        if value is not None:
            setattr(product, field_name, value)
    if args.cost is not None:
        product.cost_price = args.cost
    if args.stock is not None:
        product.stock = args.stock
    if args.keywords:
        product.keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if args.points:
        product.selling_points = [p.strip() for p in args.points.split("|") if p.strip()]
    if args.defects:
        product.defects = [d.strip() for d in args.defects.split("|") if d.strip()]

    # 送料区分が実在するか、登録時点で確かめる
    ctx.fees.shipping(product.shipping_method)

    ctx.ledger.products.upsert(product)
    _print(f"{'更新' if existing else '登録'}しました: {product.sku} / {product.name}")
    return 0


def cmd_product_list(ctx: AppContext, args: argparse.Namespace) -> int:
    products = ctx.ledger.products.all()
    if not products:
        _print("商品が登録されていません。mercari-tool product add で追加してください。")
        return 0
    columns = [("SKU", 12, "<"), ("商品名", 28, "<"), ("状態", 14, "<"),
               ("仕入値", 8, ">"), ("在庫", 4, ">"), ("配送", 14, "<")]
    header = row([(t, w, "^") for t, w, _ in columns])
    _print(header)
    _print(rule(display_width(header)))
    widths = [w for _, w, _ in columns]
    aligns = [a for _, _, a in columns]
    for product in products:
        _print(row(list(zip([
            product.sku,
            product.name,
            CONDITIONS.get(product.condition, product.condition),
            f"{product.cost_price:,}",
            f"{product.stock:,}",
            product.shipping_method,
        ], widths, aligns))))
    return 0


def cmd_product_show(ctx: AppContext, args: argparse.Namespace) -> int:
    product = _require_product(ctx, args.sku)
    _print(json.dumps(product.to_dict(), ensure_ascii=False, indent=2))
    return 0


# ══════════════════════════════════════════════════════════
# research
# ══════════════════════════════════════════════════════════
def cmd_research(ctx: AppContext, args: argparse.Namespace) -> int:
    query = args.query
    if args.sku:
        query = query or _require_product(ctx, args.sku).name
    if not query:
        return _fail("検索語か --sku のどちらかを指定してください。")

    try:
        comps = ctx.research_provider.fetch(query, limit=args.limit)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return _fail(str(exc))

    stats = analyze(comps, query=query)
    _print(format_stats(stats))

    if args.save:
        path = save_comps(ctx.config.comps_dir, query, comps)
        _print(f"\n保存しました: {path}")
    if args.json:
        _print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    return 0


# ══════════════════════════════════════════════════════════
# price
# ══════════════════════════════════════════════════════════
def _load_market(ctx: AppContext, query: str):
    from .models import MarketStats

    try:
        comps = ctx.research_provider.fetch(query, limit=200)
        return analyze(comps, query=query)
    except (FileNotFoundError, RuntimeError, ValueError):
        return MarketStats(query=query)


def cmd_price(ctx: AppContext, args: argparse.Namespace) -> int:
    product = _require_product(ctx, args.sku)
    market = _load_market(ctx, args.query or product.name)
    recommendation = ctx.pricing.recommend(product, market, other_cost=args.other_cost)

    if args.json:
        _print(json.dumps(recommendation.to_dict(), ensure_ascii=False, indent=2))
        return 0

    _print(f"■ 価格提案: {product.sku} / {product.name}")
    _print(f"  仕入値 {product.cost_price:,}円 / 配送 {ctx.fees.shipping(product.shipping_method).label}")
    if market.has_data:
        _print(f"  相場中央値 {market.median_price:,}円（{market.sample_size}件）")
    _print("")

    for option in recommendation.options:
        marker = "★" if recommendation.recommended and option.strategy == recommendation.recommended.strategy else " "
        bd = option.breakdown
        _print(f"{marker} 【{option.label}】 {option.price:,} 円")
        _print(f"    {option.rationale}")
        _print(
            f"    手数料 {bd.commission:,} / 送料 {bd.shipping_cost:,} / "
            f"梱包 {bd.packaging_cost:,} / 振込 {bd.transfer_fee:,} / 仕入 {bd.cost_price:,}"
        )
        _print(
            f"    → 純利益 {bd.net_profit:,} 円（利益率 {bd.margin_rate:.1%} / ROI {bd.roi:.1%}）"
        )
        _print("")

    _print(f"  損益分岐価格 : {recommendation.breakeven_price:,} 円（これ未満は赤字）")
    _print(f"  値下げ下限   : {recommendation.floor_price:,} 円（利益率 {ctx.config.min_margin:.0%} を確保）")
    for note in recommendation.notes:
        _print(f"  ・{note}")
    return 0


def cmd_price_offer(ctx: AppContext, args: argparse.Namespace) -> int:
    product = _require_product(ctx, args.sku)
    result = ctx.pricing.evaluate_offer(args.amount, product, other_cost=args.other_cost)

    if args.json:
        _print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    verdict_label = {
        "accept": "✅ 受けてよい",
        "counter": "🔄 逆提案する",
        "decline": "❌ 断る",
    }.get(str(result["verdict"]), str(result["verdict"]))

    _print(f"■ 値下げ交渉の判定: {product.name}")
    _print(f"  提示額     : {args.amount:,} 円")
    _print(f"  判定       : {verdict_label}")
    _print(f"  理由       : {result['reason']}")
    _print(f"  この額での利益: {result['net_profit']:,} 円（利益率 {float(result['margin_rate']):.1%}）")
    _print(f"  受けられる下限: {result['floor_price']:,} 円")
    _print(f"  損益分岐      : {result['breakeven_price']:,} 円")
    return 0


def cmd_price_maxcost(ctx: AppContext, args: argparse.Namespace) -> int:
    """想定売値から、許容できる仕入れ値の上限を出す。"""
    margin = args.margin if args.margin is not None else ctx.config.target_margin
    limit = max_viable_cost(
        ctx.pricing, args.price, args.shipping, margin, other_cost=args.other_cost
    )
    option = ctx.fees.shipping(args.shipping)
    _print("■ 仕入れ上限の逆算")
    _print(f"  想定売値   : {args.price:,} 円")
    _print(f"  配送方法   : {option.label}（{option.total:,}円）")
    _print(f"  目標利益率 : {margin:.0%}")
    _print("")
    _print(f"  → 仕入れは {limit:,} 円までなら目標を達成できます。")
    if limit <= 0:
        _print("  ⚠ この売値では、仕入れ0円でも目標利益率に届きません。")
    return 0


# ══════════════════════════════════════════════════════════
# image
# ══════════════════════════════════════════════════════════
def cmd_image_process(ctx: AppContext, args: argparse.Namespace) -> int:
    from .images import process_batch

    output = Path(args.out) if args.out else ctx.config.output_dir / "images"
    try:
        results = process_batch(
            args.files,
            output,
            size=args.size,
            whiten=args.whiten,
            tolerance=args.tolerance,
            prefix=args.prefix or "",
        )
    except (OSError, ValueError) as exc:
        return _fail(str(exc))

    for path, stats in results:
        _print(f"✔ {path}")
        if args.verbose:
            for step in stats.steps:
                _print(f"    - {step}")
    _print(f"\n{len(results)}枚を {output} に出力しました。")
    return 0


def cmd_image_thumb(ctx: AppContext, args: argparse.Namespace) -> int:
    from .images import ThumbnailBuilder
    from .images.fonts import has_japanese_font

    if not has_japanese_font() and (args.top or args.corner or args.bottom):
        _print("⚠ 日本語フォントが見つかりません。文字が □ になる可能性があります。")
        _print("  MERCARI_FONT_PATH で .ttf/.otf のパスを指定できます。")

    output = Path(args.out) if args.out else (
        ctx.config.output_dir / f"{Path(args.photo).stem}_thumb.jpg"
    )
    try:
        path = ThumbnailBuilder.build(
            args.photo,
            output,
            top_text=args.top or "",
            corner_text=args.corner or "",
            bottom_text=args.bottom or "",
            palette=args.palette,
            canvas_size=args.size,
            blurred_backdrop=not args.plain,
        )
    except (OSError, ValueError) as exc:
        return _fail(str(exc))
    _print(f"✔ サムネイルを作成しました: {path}")
    return 0


# ══════════════════════════════════════════════════════════
# draft
# ══════════════════════════════════════════════════════════
def cmd_draft(ctx: AppContext, args: argparse.Namespace) -> int:
    from .export import DraftBuilder, write_draft_json, write_listing_text

    product = _require_product(ctx, args.sku)
    builder = DraftBuilder(ctx)
    result = builder.build(
        product,
        photos=args.photos or [],
        research_query=args.query,
        strategy=args.strategy or "",
        whiten_background=args.whiten,
        generate_copy=not args.no_copy,
        output_dir=args.out,
    )

    target = result.output_dir or ctx.config.output_dir / product.sku
    shipping_label = ctx.fees.shipping(product.shipping_method).label
    text_path = write_listing_text(result.draft, target / "listing.txt", shipping_label)
    json_path = write_draft_json(result, target / "draft.json")

    _print(f"■ 出品ドラフト: {product.sku} / {product.name}\n")
    for step in result.steps:
        _print(f"  ✔ {step}")
    if result.notes:
        _print("")
        for note in result.notes:
            _print(f"  ・{note}")
    if result.warnings:
        _print("")
        for warning in result.warnings:
            _print(f"  {warning if warning.startswith('⚠') else '⚠ ' + warning}")

    _print("")
    _print("─" * 56)
    _print(f"タイトル : {result.draft.title}（{len(result.draft.title)}文字）")
    _print(f"価格     : {result.draft.price:,} 円")
    if result.draft.catchphrase:
        _print(f"キャッチ : {result.draft.catchphrase}")
    if result.draft.description:
        _print("\n" + result.draft.description)
    if result.draft.hashtags:
        _print("\n" + " ".join(f"#{t}" for t in result.draft.hashtags))
    _print("─" * 56)
    _print(f"\n出品用テキスト: {text_path}")
    _print(f"ドラフトJSON  : {json_path}")
    if result.draft.thumbnail_path:
        _print(f"サムネイル    : {result.draft.thumbnail_path}")
    return 0


# ══════════════════════════════════════════════════════════
# comment
# ══════════════════════════════════════════════════════════
def cmd_comment(ctx: AppContext, args: argparse.Namespace) -> int:
    product = _require_product(ctx, args.sku)

    comments: list[str] = []
    if args.text:
        comments.append(args.text)
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
        comments.extend([line.strip() for line in content.splitlines() if line.strip()])
    if not comments:
        return _fail("--text か --file でコメントを指定してください。")

    price = args.price or product.cost_price
    if not args.price:
        recommendation = ctx.pricing.recommend(product, _load_market(ctx, product.name))
        if recommendation.recommended:
            price = recommendation.recommended.price

    shipping_label = ctx.fees.shipping(product.shipping_method).label
    replies = ctx.responder.respond_many(comments, product, price, shipping_label)

    if args.json:
        _print(json.dumps([r.to_dict() for r in replies], ensure_ascii=False, indent=2))
        return 0

    for reply in replies:
        _print("─" * 56)
        _print(f"受信コメント: {reply.comment}")
        _print(f"意図        : {reply.intent.label}")
        if reply.offered_price:
            _print(f"提示額      : {reply.offered_price:,} 円")
        if reply.price_verdict:
            _print(f"価格判定    : {reply.price_verdict.get('verdict')} — {reply.price_verdict.get('reason')}")
        flag = "自動送信OK" if reply.auto_send_ok else "⚠ 送信前に確認してください"
        _print(f"判定        : {flag}（生成元: {reply.source}）")
        _print("")
        _print(reply.reply)
        _print("")
    return 0


# ══════════════════════════════════════════════════════════
# supplier / sourcing
# ══════════════════════════════════════════════════════════
def cmd_supplier_add(ctx: AppContext, args: argparse.Namespace) -> int:
    existing = ctx.suppliers.get(args.id) if args.id else None
    supplier = existing or Supplier(name=args.name or "")
    if args.id:
        supplier.id = args.id
    for field_name, value in (
        ("name", args.name),
        ("channel", args.channel),
        ("url", args.url),
        ("notes", args.notes),
    ):
        if value is not None:
            setattr(supplier, field_name, value)
    for field_name, value in (
        ("lead_time_days", args.lead_time),
        ("min_order_qty", args.min_qty),
        ("avg_unit_cost", args.unit_cost),
        ("order_shipping_cost", args.order_shipping),
        ("order_count", args.order_count),
    ):
        if value is not None:
            setattr(supplier, field_name, value)
    if args.defect_rate is not None:
        supplier.defect_rate = args.defect_rate
    if args.reliability is not None:
        supplier.reliability = args.reliability

    ctx.suppliers.upsert(supplier)
    _print(f"{'更新' if existing else '登録'}しました: {supplier.id} / {supplier.name}")
    return 0


def cmd_supplier_list(ctx: AppContext, args: argparse.Namespace) -> int:
    suppliers = ctx.suppliers.all()
    if not suppliers:
        _print("仕入れ先が登録されていません。mercari-tool supplier add で追加してください。")
        return 0
    columns = [("ID", 16, "<"), ("名前", 20, "<"), ("チャネル", 16, "<"),
               ("単価", 8, ">"), ("ロット", 6, ">"), ("納期", 6, ">"),
               ("不良率", 7, ">"), ("評価", 5, ">"), ("取引", 5, ">")]
    header = row([(t, w, "^") for t, w, _ in columns])
    _print(header)
    _print(rule(display_width(header)))
    widths = [w for _, w, _ in columns]
    aligns = [a for _, _, a in columns]
    for supplier in suppliers:
        _print(row(list(zip([
            supplier.id,
            supplier.name,
            supplier.channel,
            f"{supplier.avg_unit_cost:,}",
            f"{supplier.min_order_qty:,}",
            f"{supplier.lead_time_days}日",
            f"{supplier.defect_rate:.1%}",
            f"{supplier.reliability:.1f}",
            f"{supplier.order_count:,}",
        ], widths, aligns))))
    return 0


def cmd_sourcing_rank(ctx: AppContext, args: argparse.Namespace) -> int:
    suppliers = ctx.suppliers.all()
    if not suppliers:
        return _fail("仕入れ先が登録されていません。mercari-tool supplier add で追加してください。")

    product = ctx.ledger.products.get(args.sku) if args.sku else None
    query = args.query or (product.name if product else "")
    market = _load_market(ctx, query) if query else None

    sale_price = args.price
    if not sale_price and market and market.has_data:
        sale_price = market.median_price
    if not sale_price:
        return _fail("想定売値が決まりません。--price で指定するか、相場データを用意してください。")

    shipping = args.shipping or (product.shipping_method if product else "nekopos")
    only = set(args.suppliers.split(",")) if args.suppliers else None

    candidates = [
        SourcingCandidate(
            supplier=s,
            product_name=product.name if product else query,
            unit_cost=args.unit_cost or s.avg_unit_cost,
            expected_sale_price=sale_price,
            shipping_method=shipping,
        )
        for s in suppliers
        if (only is None or s.id in only) and (args.unit_cost or s.avg_unit_cost) > 0
    ]
    if not candidates:
        return _fail("評価できる仕入れ先がありません（単価が未設定の可能性があります）。")

    results = ctx.evaluator.rank(candidates, market)
    if args.json:
        _print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
        return 0

    _print(format_results(results))
    limit = max_viable_cost(ctx.pricing, sale_price, shipping, ctx.config.target_margin)
    _print(f"参考: 想定売値 {sale_price:,}円 / 目標利益率 {ctx.config.target_margin:.0%} なら")
    _print(f"      仕入れは {limit:,} 円までが上限です。")
    return 0


# ══════════════════════════════════════════════════════════
# sales
# ══════════════════════════════════════════════════════════
def cmd_sales_add(ctx: AppContext, args: argparse.Namespace) -> int:
    sale = ctx.ledger.record_sale(
        sku=args.sku,
        sale_price=args.price,
        sold_at=_parse_day(args.date),
        listed_at=_parse_day(args.listed),
        cost_price=args.cost,
        shipping_method=args.shipping,
        other_cost=args.other_cost,
        memo=args.memo or "",
    )
    _print(f"売上を記録しました: {sale.sku} / {sale.sale_price:,}円")
    _print(
        f"  手数料 {sale.commission:,} / 送料 {sale.shipping_cost:,} / "
        f"梱包 {sale.packaging_cost:,} / その他 {sale.other_cost:,} / 仕入 {sale.cost_price:,}"
    )
    _print(f"  → 純利益 {sale.net_profit:,} 円（利益率 {sale.margin_rate:.1%}）")

    if args.sync:
        return _sync_sheets(ctx)
    return 0


def cmd_sales_buy(ctx: AppContext, args: argparse.Namespace) -> int:
    purchase = ctx.ledger.record_purchase(
        sku=args.sku,
        unit_cost=args.cost,
        quantity=args.qty,
        supplier_id=args.supplier or "",
        shipping_cost=args.shipping_cost,
        other_cost=args.other_cost,
        purchased_at=_parse_day(args.date),
        memo=args.memo or "",
    )
    _print(f"仕入れを記録しました: {purchase.sku} × {purchase.quantity}")
    _print(f"  合計 {purchase.total_cost:,} 円 / 実質単価 {purchase.unit_cost_landed:,} 円")
    if args.sync:
        return _sync_sheets(ctx)
    return 0


def cmd_sales_report(ctx: AppContext, args: argparse.Namespace) -> int:
    start, end = _parse_day(args.start), _parse_day(args.end)
    if args.days:
        end = end or date.today()
        start = start or (end - timedelta(days=args.days - 1))

    sales = ctx.ledger.sales_between(start, end)
    purchases = ctx.ledger.purchases_between(start, end)

    if not sales and not purchases:
        _print("対象期間のデータがありません。")
        return 0

    if args.json:
        payload = {
            "daily": [r.to_dict() for r in daily_summary(sales, purchases)],
            "monthly": [r.to_dict() for r in monthly_summary(sales, purchases)],
            "overall": overall_summary(sales, purchases).to_dict(),
            "by_category": [g.to_dict() for g in group_by(sales, "category")],
        }
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    period = ""
    if start or end:
        period = f"（{start or '最初'} 〜 {end or '最新'}）"

    if args.mode in ("daily", "both"):
        _print(format_summary_table(daily_summary(sales, purchases), f"■ 日次推移{period}"))
        _print("")
    if args.mode in ("monthly", "both"):
        rows = monthly_summary(sales, purchases)
        _print(format_summary_table(rows, f"■ 月次推移{period}"))
        for note in trend_note(rows):
            _print(f"  ・{note}")
        _print("")

    if args.by:
        labels = {s.id: s.name for s in ctx.suppliers} if args.by == "supplier_id" else None
        title = {"category": "■ カテゴリ別", "supplier_id": "■ 仕入れ先別", "sku": "■ 商品別"}
        _print(format_group_table(group_by(sales, args.by, labels), title.get(args.by, "■ 集計")))
        _print("")

    total = overall_summary(sales, purchases)
    _print("■ 合計")
    _print(f"  売上           : {total.revenue:,} 円（{total.sales_count} 件）")
    _print(f"  売上原価（仕入）: {total.cogs:,} 円")
    _print(f"  販売手数料     : {total.commission:,} 円")
    _print(f"  送料           : {total.shipping:,} 円")
    _print(f"  梱包・その他   : {total.packaging + total.other:,} 円")
    _print("  ────────────────────────────")
    _print(f"  純利益         : {total.net_profit:,} 円（利益率 {total.margin_rate:.1%}）")
    _print("")
    _print(f"  期間内の仕入支払: {total.purchase_amount:,} 円（{total.purchase_count} 件）")
    _print(f"  現金収支       : {total.cash_flow:,} 円")

    inventory = ctx.ledger.inventory()
    if inventory:
        tied = sum(int(i["tied_up"]) for i in inventory)
        _print(f"  在庫（未販売） : {len(inventory)} 品 / {tied:,} 円が在庫に滞留")
    return 0


def cmd_sales_export(ctx: AppContext, args: argparse.Namespace) -> int:
    from .export import export_sales_csv

    sales = ctx.ledger.sales_between(_parse_day(args.start), _parse_day(args.end))
    path = Path(args.out) if args.out else ctx.config.output_dir / "sales.csv"
    export_sales_csv(sales, path)
    _print(f"✔ {len(sales)} 件を書き出しました: {path}")
    return 0


def cmd_sales_import(ctx: AppContext, args: argparse.Namespace) -> int:
    try:
        count = ctx.ledger.import_sales_csv(args.file)
    except (OSError, ValueError) as exc:
        return _fail(str(exc))
    _print(f"✔ {count} 件を取り込みました。")
    if args.sync:
        return _sync_sheets(ctx)
    return 0


def _sync_sheets(ctx: AppContext) -> int:
    from .sales.sheets import SheetsError, SheetsSync

    try:
        result = SheetsSync(ctx.config).sync(ctx.ledger)
    except SheetsError as exc:
        return _fail(str(exc))
    _print("✔ スプレッドシートに反映しました")
    _print(f"  URL      : {result['url']}")
    _print(f"  売上明細 : {result['sales_rows']} 行")
    _print(f"  仕入明細 : {result['purchase_rows']} 行")
    _print(f"  日次推移 : {result['daily_rows']} 行")
    _print(f"  月次推移 : {result['monthly_rows']} 行")
    return 0


def cmd_sales_sync(ctx: AppContext, args: argparse.Namespace) -> int:
    return _sync_sheets(ctx)


# ══════════════════════════════════════════════════════════
# パーサ組み立て
# ══════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mercari-tool",
        description="メルカリ出品支援エンジン",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"mercari-tool {__version__}")
    parser.add_argument("--env", help=".env ファイルのパス")
    parser.add_argument("--data-dir", help="データディレクトリ（既定: ./data）")

    sub = parser.add_subparsers(dest="group", metavar="グループ")

    # init / fees
    p = sub.add_parser("init", help="データディレクトリと .env を用意する")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("fees", help="手数料・送料テーブルを表示する")
    p.set_defaults(func=cmd_fees)

    # ── product ────────────────────────────────────────
    product = sub.add_parser("product", help="商品マスタ").add_subparsers(
        dest="action", metavar="コマンド"
    )
    p = product.add_parser("add", help="商品を登録・更新する")
    p.add_argument("--sku", required=True)
    p.add_argument("--name")
    p.add_argument("--category")
    p.add_argument("--brand")
    p.add_argument("--condition", choices=list(CONDITIONS), help="商品の状態")
    p.add_argument("--cost", type=int, help="仕入れ値（円）")
    p.add_argument("--stock", type=int, help="在庫数")
    p.add_argument("--shipping", help="配送方法キー（mercari-tool fees で一覧）")
    p.add_argument("--size", help="サイズ・寸法のメモ")
    p.add_argument("--keywords", help="検索キーワード（カンマ区切り）")
    p.add_argument("--points", help="訴求ポイント（| 区切り）")
    p.add_argument("--defects", help="傷・汚れなど（| 区切り）")
    p.add_argument("--supplier", help="仕入れ先ID")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_product_add)

    p = product.add_parser("list", help="商品一覧")
    p.set_defaults(func=cmd_product_list)
    p = product.add_parser("show", help="商品の詳細を JSON で表示")
    p.add_argument("sku")
    p.set_defaults(func=cmd_product_show)

    # ── research ───────────────────────────────────────
    p = sub.add_parser("research", help="相場をリサーチする")
    p.add_argument("query", nargs="?", help="検索語")
    p.add_argument("--sku", help="商品名を検索語として使う")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--save", action="store_true", help="取得結果を保存する")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_research)

    # ── price ──────────────────────────────────────────
    price = sub.add_parser("price", help="価格設定").add_subparsers(
        dest="action", metavar="コマンド"
    )
    p = price.add_parser("suggest", help="価格を提案する")
    p.add_argument("sku")
    p.add_argument("--query", help="相場検索の語（既定: 商品名）")
    p.add_argument("--other-cost", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_price)

    p = price.add_parser("offer", help="値下げ交渉に応じるか判定する")
    p.add_argument("sku")
    p.add_argument("amount", type=int, help="提示された金額")
    p.add_argument("--other-cost", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_price_offer)

    p = price.add_parser("maxcost", help="想定売値から仕入れ上限を逆算する")
    p.add_argument("--price", type=int, required=True, help="想定売値")
    p.add_argument("--shipping", default="nekopos")
    p.add_argument("--margin", type=float, help="目標利益率（0.3 = 30%%）")
    p.add_argument("--other-cost", type=int, default=0)
    p.set_defaults(func=cmd_price_maxcost)

    # ── image ──────────────────────────────────────────
    image = sub.add_parser("image", help="画像加工").add_subparsers(
        dest="action", metavar="コマンド"
    )
    p = image.add_parser("process", help="出品写真を自動補正する")
    p.add_argument("files", nargs="+")
    p.add_argument("--out", help="出力先ディレクトリ")
    p.add_argument("--size", type=int, default=1080)
    p.add_argument("--whiten", action="store_true", help="背景を白飛ばしする")
    p.add_argument("--tolerance", type=int, default=42, help="背景判定の許容差")
    p.add_argument("--prefix", help="出力ファイル名の接頭辞")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_image_process)

    p = image.add_parser("thumb", help="サムネイルを作る")
    p.add_argument("photo")
    p.add_argument("--out")
    p.add_argument("--top", help="上部の帯テキスト（例: 送料無料）")
    p.add_argument("--corner", help="右上のバッジ（例: 美品）")
    p.add_argument("--bottom", help="下部の帯テキスト")
    p.add_argument("--palette", default="red", choices=["red", "navy", "green", "orange", "black", "yellow"])
    p.add_argument("--size", type=int, default=1080)
    p.add_argument("--plain", action="store_true", help="背景ぼかしを使わない")
    p.set_defaults(func=cmd_image_thumb)

    # ── draft ──────────────────────────────────────────
    p = sub.add_parser("draft", help="出品ドラフトを一括生成する")
    p.add_argument("sku")
    p.add_argument("--photos", nargs="*", help="出品写真のパス")
    p.add_argument("--query", help="相場検索の語")
    p.add_argument("--strategy", choices=["quick", "balanced", "profit"])
    p.add_argument("--whiten", action="store_true", help="写真の背景を白飛ばしする")
    p.add_argument("--no-copy", action="store_true", help="文章生成をスキップする")
    p.add_argument("--out", help="出力先ディレクトリ")
    p.set_defaults(func=cmd_draft)

    # ── comment ────────────────────────────────────────
    p = sub.add_parser("comment", help="コメント返信文を生成する")
    p.add_argument("sku")
    p.add_argument("--text", help="受信したコメント")
    p.add_argument("--file", help="1行1コメントのテキストファイル")
    p.add_argument("--price", type=int, help="現在の出品価格")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_comment)

    # ── supplier ───────────────────────────────────────
    supplier = sub.add_parser("supplier", help="仕入れ先マスタ").add_subparsers(
        dest="action", metavar="コマンド"
    )
    p = supplier.add_parser("add", help="仕入れ先を登録・更新する")
    p.add_argument("--id", help="更新する場合のID")
    p.add_argument("--name")
    p.add_argument("--channel", help="ネット卸 / リサイクルショップ / 問屋 など")
    p.add_argument("--url")
    p.add_argument("--lead-time", type=int, help="到着までの日数")
    p.add_argument("--min-qty", type=int, help="最低ロット")
    p.add_argument("--unit-cost", type=int, help="平均仕入れ単価")
    p.add_argument("--order-shipping", type=int, help="1回の発注にかかる送料")
    p.add_argument("--defect-rate", type=float, help="不良率（0.05 = 5%%）")
    p.add_argument("--reliability", type=float, help="信頼度 0-5")
    p.add_argument("--order-count", type=int, help="これまでの取引回数")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_supplier_add)

    p = supplier.add_parser("list", help="仕入れ先一覧")
    p.set_defaults(func=cmd_supplier_list)

    # ── sourcing ───────────────────────────────────────
    sourcing = sub.add_parser("sourcing", help="仕入れ判断").add_subparsers(
        dest="action", metavar="コマンド"
    )
    p = sourcing.add_parser("rank", help="仕入れ先を評価して並べる")
    p.add_argument("--sku", help="対象商品の SKU")
    p.add_argument("--query", help="相場検索の語")
    p.add_argument("--price", type=int, help="想定売値（未指定なら相場中央値）")
    p.add_argument("--unit-cost", type=int, help="全候補に共通の仕入れ単価")
    p.add_argument("--shipping", help="配送方法キー")
    p.add_argument("--suppliers", help="評価対象の仕入れ先ID（カンマ区切り）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sourcing_rank)

    # ── sales ──────────────────────────────────────────
    sales = sub.add_parser("sales", help="売上・仕入れ管理").add_subparsers(
        dest="action", metavar="コマンド"
    )
    p = sales.add_parser("add", help="売上を記録する")
    p.add_argument("--sku", required=True)
    p.add_argument("--price", type=int, required=True, help="販売価格")
    p.add_argument("--cost", type=int, help="仕入れ値（未指定なら商品マスタから）")
    p.add_argument("--date", help="売却日 YYYY-MM-DD（既定: 今日）")
    p.add_argument("--listed", help="出品日 YYYY-MM-DD")
    p.add_argument("--shipping", help="配送方法キー")
    p.add_argument("--other-cost", type=int, default=0)
    p.add_argument("--memo")
    p.add_argument("--sync", action="store_true", help="記録後にスプレッドシートへ反映")
    p.set_defaults(func=cmd_sales_add)

    p = sales.add_parser("buy", help="仕入れを記録する")
    p.add_argument("--sku", required=True)
    p.add_argument("--cost", type=int, required=True, help="仕入れ単価")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--supplier", help="仕入れ先ID")
    p.add_argument("--shipping-cost", type=int, default=0)
    p.add_argument("--other-cost", type=int, default=0)
    p.add_argument("--date", help="仕入れ日 YYYY-MM-DD")
    p.add_argument("--memo")
    p.add_argument("--sync", action="store_true")
    p.set_defaults(func=cmd_sales_buy)

    p = sales.add_parser("report", help="売上を集計する")
    p.add_argument("--mode", choices=["daily", "monthly", "both"], default="both")
    p.add_argument("--start", help="開始日 YYYY-MM-DD")
    p.add_argument("--end", help="終了日 YYYY-MM-DD")
    p.add_argument("--days", type=int, help="直近N日")
    p.add_argument("--by", choices=["category", "supplier_id", "sku"], help="切り口別の集計")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sales_report)

    p = sales.add_parser("sync", help="Googleスプレッドシートへ反映する")
    p.set_defaults(func=cmd_sales_sync)

    p = sales.add_parser("export", help="売上明細を CSV に書き出す")
    p.add_argument("--out")
    p.add_argument("--start")
    p.add_argument("--end")
    p.set_defaults(func=cmd_sales_export)

    p = sales.add_parser("import", help="売上を CSV から取り込む")
    p.add_argument("file")
    p.add_argument("--sync", action="store_true")
    p.set_defaults(func=cmd_sales_import)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        # グループだけ指定された場合は、そのグループのヘルプを出す
        parser.print_help()
        return 1

    config = Config.load(args.env)
    if args.data_dir:
        config.data_dir = Path(args.data_dir)
    context = AppContext(config)

    try:
        return args.func(context, args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    except (KeyError, ValueError, OSError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
