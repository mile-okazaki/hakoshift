"""プロンプトと、モデルに渡す商品コンテキストの組み立て。"""

from __future__ import annotations

from ..models import CONDITIONS, MarketStats, Product

#: メルカリの入力上限
TITLE_MAX_CHARS = 40
DESCRIPTION_MAX_CHARS = 1000

COPY_SYSTEM_PROMPT = """あなたはメルカリでの販売実績が豊富な出品者です。
検索で見つけてもらえて、かつ購入につながる出品文を書きます。

守ること:
- タイトルは全角40文字以内。検索されるキーワードを前半に詰める。
- 記号の羅列（★☆■◆など）や過剰な煽り文句は使わない。読みにくく、信頼も落ちる。
- 事実でないことは書かない。「新品同様」「未使用に近い」などの表現は、
  与えられた商品状態と矛盾しない範囲でだけ使う。
- 保証できないこと（正規品保証、動作永久保証など）は書かない。
- 傷や汚れは隠さず具体的に書く。トラブルを防ぐことが最終的に評価を守る。
- 医薬品的な効能、真贋の断定、法令に触れる表現は避ける。

日本語で出力します。"""

COMMENT_SYSTEM_PROMPT = """あなたはメルカリの出品者として、購入検討者のコメントに返信します。

守ること:
- 丁寧語で、簡潔に。3〜4文以内が基本。
- 相手の質問に必ず答える。答えられないことは「確認します」と正直に書く。
- 値下げ交渉には、こちらが提示された条件を受けられるかどうかの判断結果に従う。
  判断結果を無視して勝手に値引きを約束しない。
- 在庫や発送日など、こちらが把握していない情報を断定しない。
- 過度にへりくだらない。取引相手として対等な、落ち着いた文面にする。
- 挨拶と締めは短く。定型の繰り返しは冗長なので避ける。

日本語で出力します。"""


def product_context(product: Product, market: MarketStats | None = None) -> str:
    """商品と相場をモデルに渡す共通コンテキスト。"""
    lines = [
        "## 商品情報",
        f"- 商品名: {product.name}",
        f"- カテゴリ: {product.category or '(未設定)'}",
        f"- ブランド: {product.brand or '(なし/不明)'}",
        f"- 商品の状態: {CONDITIONS.get(product.condition, product.condition)}",
    ]
    if product.size_note:
        lines.append(f"- サイズ・寸法: {product.size_note}")
    if product.keywords:
        lines.append(f"- 想定検索キーワード: {', '.join(product.keywords)}")
    if product.selling_points:
        lines.append("- 訴求ポイント:")
        lines.extend(f"    - {p}" for p in product.selling_points)
    if product.defects:
        lines.append("- 正直に書くべき難点（省略・美化しないこと）:")
        lines.extend(f"    - {d}" for d in product.defects)
    if product.notes:
        lines.append(f"- 補足: {product.notes}")

    if market and market.has_data:
        lines += [
            "",
            "## 相場データ",
            f"- サンプル {market.sample_size} 件（売却済み {market.sold_count} 件、売却率 {market.sell_through_rate:.0%}）",
            f"- 成約価格の中央値: {market.median_price:,} 円（{market.p25_price:,}〜{market.p75_price:,} 円）",
        ]
        if market.hot_keywords:
            lines.append(
                f"- 売れている出品タイトルに多い語: {', '.join(market.hot_keywords)}"
            )
            lines.append(
                "  ※ これらのうち、この商品に事実として当てはまる語だけをタイトルに入れること。"
            )
    return "\n".join(lines)


# ── JSON Schema ─────────────────────────────────────────────
TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "reason": {"type": "string"},
                    "keywords_used": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "reason", "keywords_used"],
                "additionalProperties": False,
            },
        },
        "catchphrases": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titles", "catchphrases"],
    "additionalProperties": False,
}

DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "highlights": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "hashtags", "highlights"],
    "additionalProperties": False,
}

COMMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "intent": {
            "type": "string",
            "enum": [
                "price_negotiation",
                "stock_check",
                "shipping",
                "condition_question",
                "reserve_request",
                "complaint",
                "other",
            ],
        },
        "requires_human": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["reply", "intent", "requires_human", "reason"],
    "additionalProperties": False,
}
