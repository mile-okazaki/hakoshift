"""コメント返信。

流れ:
  1. ルールで意図を分類する（値下げ交渉か、在庫確認か…）
  2. 値下げ交渉なら金額を抽出し、価格エンジンで受諾可否を先に決める
  3. その判断結果を渡して、Claude に返信文を書かせる
  4. API が使えない場合はテンプレートにフォールバックする

「いくらまで下げるか」をモデルの気分に任せないのが要点。金額の判断は
価格エンジンが行い、モデルは文章化だけを担当する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..llm import LLMClient, LLMError
from ..models import CONDITIONS, Product
from ..pricing import PricingEngine
from .prompts import COMMENT_SCHEMA, COMMENT_SYSTEM_PROMPT


class CommentIntent(str, Enum):
    PRICE_NEGOTIATION = "price_negotiation"
    STOCK_CHECK = "stock_check"
    SHIPPING = "shipping"
    CONDITION_QUESTION = "condition_question"
    RESERVE_REQUEST = "reserve_request"
    COMPLAINT = "complaint"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            CommentIntent.PRICE_NEGOTIATION: "値下げ交渉",
            CommentIntent.STOCK_CHECK: "在庫確認",
            CommentIntent.SHIPPING: "発送・配送について",
            CommentIntent.CONDITION_QUESTION: "商品状態の質問",
            CommentIntent.RESERVE_REQUEST: "専用・取り置き依頼",
            CommentIntent.COMPLAINT: "クレーム・トラブル",
            CommentIntent.OTHER: "その他",
        }[self]


#: 意図判定のキーワード。上から順に評価し、最初に当たったものを採用する。
_INTENT_PATTERNS: list[tuple[CommentIntent, re.Pattern[str]]] = [
    (
        CommentIntent.COMPLAINT,
        re.compile(
            r"届(か|き)ま?せ|返品|返金|壊れ|破損|不良|違う商品|説明と違|キャンセル|クレーム"
        ),
    ),
    (
        CommentIntent.PRICE_NEGOTIATION,
        re.compile(
            r"値下げ|お値引|値引き|安く|お安く|即決|円に(なり|して)|まで(なら|は)?下"
            # 「3000円希望です」「3,500円でいかがでしょう」のように、
            # 「値下げ」の語が無くても金額の提示は交渉として扱う
            r"|[0-9][0-9,.]*\s*万?円.{0,8}(希望|お願い|可能|いかが|どう|ませ)"
        ),
    ),
    (CommentIntent.RESERVE_REQUEST, re.compile(r"専用|取り?置き|おまとめ|まとめ買い|お取り置")),
    (
        CommentIntent.SHIPPING,
        re.compile(r"発送|配送|送料|いつ届|何日|到着|日時指定|匿名|梱包"),
    ),
    # 「傷はありますか」を在庫確認に取られないよう、状態の質問を先に判定する。
    # 在庫確認の語（在庫・まだ・購入可能）はここには含まれないので取り違えない。
    (
        CommentIntent.CONDITION_QUESTION,
        re.compile(
            r"傷|キズ|汚れ|使用感|動作|状態|サイズ|寸法|何cm|色|付属|箱は|説明書|型番|年式"
        ),
    ),
    (
        CommentIntent.STOCK_CHECK,
        re.compile(r"在庫|まだ(あり|残|販売)|購入(可能|できま)|売れて|ありますか"),
    ),
]

#: 「3,000円」「3000円で」などから金額を拾う
_PRICE_PATTERN = re.compile(r"([0-9][0-9,]{1,9})\s*(?:円|えん)")
#: 「1.5万」「2万円」
_MAN_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*万")


def classify_intent(comment: str) -> CommentIntent:
    """コメント本文から意図を推定する。"""
    text = comment.strip()
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(text):
            return intent
    return CommentIntent.OTHER


def extract_offered_price(comment: str) -> int | None:
    """コメントから提示金額を取り出す。見つからなければ None。"""
    man = _MAN_PATTERN.search(comment)
    if man:
        try:
            return int(float(man.group(1)) * 10_000)
        except ValueError:
            pass
    matches = _PRICE_PATTERN.findall(comment)
    prices = []
    for raw in matches:
        try:
            prices.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    # 複数出てきたら最も低い額を提示額とみなす（相手の希望額）
    return min(prices) if prices else None


@dataclass
class CommentReply:
    """1件のコメントに対する返信案。"""

    comment: str = ""
    intent: CommentIntent = CommentIntent.OTHER
    reply: str = ""
    #: 人の確認なしに送ってよいか
    auto_send_ok: bool = False
    reason: str = ""
    offered_price: int | None = None
    #: 値下げ交渉のときの価格判定結果
    price_verdict: dict[str, Any] = field(default_factory=dict)
    source: str = "llm"  # llm / template

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment": self.comment,
            "intent": self.intent.value,
            "intent_label": self.intent.label,
            "reply": self.reply,
            "auto_send_ok": self.auto_send_ok,
            "reason": self.reason,
            "offered_price": self.offered_price,
            "price_verdict": self.price_verdict,
            "source": self.source,
        }


# ── テンプレート（API が使えないときのフォールバック）────────────
_TEMPLATES: dict[CommentIntent, str] = {
    CommentIntent.STOCK_CHECK: (
        "コメントありがとうございます。\n"
        "こちら在庫ございます。そのままご購入いただけます。\n"
        "ご検討よろしくお願いいたします。"
    ),
    CommentIntent.SHIPPING: (
        "コメントありがとうございます。\n"
        "{shipping_label}での発送を予定しております。\n"
        "ご購入いただいてから1〜2日以内に発送いたします。"
    ),
    CommentIntent.CONDITION_QUESTION: (
        "コメントありがとうございます。\n"
        "商品の状態は「{condition_label}」です。{defects_note}\n"
        "他にご不明な点があればお知らせください。"
    ),
    CommentIntent.RESERVE_REQUEST: (
        "コメントありがとうございます。\n"
        "専用ページの作成は承っておりますが、お取り置き中も他の方のご購入が可能です。\n"
        "恐れ入りますが、先にご購入いただいた方を優先とさせていただきます。"
    ),
    CommentIntent.COMPLAINT: (
        "ご連絡ありがとうございます。ご不便をおかけし申し訳ございません。\n"
        "状況を確認したうえで、改めてご連絡いたします。\n"
        "お手数ですが、該当箇所のお写真をお送りいただけますでしょうか。"
    ),
    CommentIntent.OTHER: (
        "コメントありがとうございます。\n"
        "確認のうえ、改めてご返信いたします。少々お待ちください。"
    ),
}


class CommentResponder:
    """コメントを読み、返信案を作る。"""

    def __init__(
        self,
        llm: LLMClient | None = None,
        pricing: PricingEngine | None = None,
    ) -> None:
        self.llm = llm
        self.pricing = pricing

    # ── 本体 ──────────────────────────────────────────────
    def respond(
        self,
        comment: str,
        product: Product,
        listing_price: int,
        shipping_label: str = "",
    ) -> CommentReply:
        """コメント1件に対する返信案を作る。"""
        intent = classify_intent(comment)
        offered = (
            extract_offered_price(comment)
            if intent == CommentIntent.PRICE_NEGOTIATION
            else None
        )

        # 値下げ交渉は、文章より先に「受けられるか」を数字で決める
        verdict: dict[str, Any] = {}
        if intent == CommentIntent.PRICE_NEGOTIATION and self.pricing:
            if offered:
                verdict = self.pricing.evaluate_offer(offered, product)
            else:
                # 金額の提示がない「お値下げ可能ですか？」への回答用に下限だけ出す
                floor = self.pricing.price_for_margin(
                    self.pricing.min_margin,
                    cost_price=product.cost_price,
                    shipping_method=product.shipping_method,
                )
                verdict = {
                    "verdict": "ask_amount",
                    "reason": "提示金額が不明です。希望額を聞き返します。",
                    "floor_price": floor,
                }

        if self.llm and self.llm.is_available():
            try:
                return self._respond_with_llm(
                    comment, product, listing_price, intent, offered, verdict, shipping_label
                )
            except LLMError:
                pass  # API が落ちていてもテンプレートで返せるようにする

        return self._respond_with_template(
            comment, product, intent, offered, verdict, shipping_label
        )

    def respond_many(
        self,
        comments: list[str],
        product: Product,
        listing_price: int,
        shipping_label: str = "",
    ) -> list[CommentReply]:
        return [
            self.respond(c, product, listing_price, shipping_label) for c in comments
        ]

    # ── LLM 経由 ───────────────────────────────────────────
    def _respond_with_llm(
        self,
        comment: str,
        product: Product,
        listing_price: int,
        intent: CommentIntent,
        offered: int | None,
        verdict: dict[str, Any],
        shipping_label: str,
    ) -> CommentReply:
        assert self.llm is not None

        facts = [
            f"- 商品名: {product.name}",
            f"- 出品価格: {listing_price:,} 円",
            f"- 商品の状態: {CONDITIONS.get(product.condition, product.condition)}",
            f"- 在庫: {product.stock} 点",
        ]
        if shipping_label:
            facts.append(f"- 配送方法: {shipping_label}")
        if product.defects:
            facts.append(f"- 申告済みの難点: {' / '.join(product.defects)}")
        if product.size_note:
            facts.append(f"- サイズ: {product.size_note}")

        verdict_block = ""
        if verdict:
            mapping = {
                "accept": f"提示額 {offered:,} 円を **受諾する**。この金額に変更する旨を伝える。",
                "counter": (
                    f"提示額 {offered:,} 円は受けられない。"
                    f"代わりに {verdict.get('floor_price', 0):,} 円なら可能、と逆提案する。"
                ),
                "decline": (
                    f"提示額 {offered:,} 円は赤字になるため **断る**。"
                    f"{verdict.get('floor_price', 0):,} 円が限界であることを丁寧に伝える。"
                ),
                "ask_amount": "希望金額の提示がないため、いくらをご希望か聞き返す。",
            }
            verdict_block = f"""
## 値下げ交渉の判断（この判断に必ず従うこと）
{mapping.get(str(verdict.get('verdict')), '判断不能')}
判断理由: {verdict.get('reason', '')}

金額はここで決まっています。これ以外の金額を返信文に書かないでください。
"""

        user = f"""## 出品中の商品
{chr(10).join(facts)}

## 届いたコメント
「{comment}」

## 事前分類
- 意図: {intent.label}
{verdict_block}
## 依頼
このコメントへの返信文を書いてください。
上の「事実」に無い情報（在庫数、発送日、傷の有無など）を勝手に断定しないこと。
判断に迷う内容や、こちらが確認しないと答えられない内容であれば
requires_human を true にしてください。
"""
        data = self.llm.complete_json(
            COMMENT_SYSTEM_PROMPT, user, COMMENT_SCHEMA, max_tokens=4000, effort="low"
        )

        requires_human = bool(data.get("requires_human", False))
        # クレームと値下げの受諾・辞退は、必ず人が目を通してから送る
        if intent in (CommentIntent.COMPLAINT, CommentIntent.PRICE_NEGOTIATION):
            requires_human = True

        return CommentReply(
            comment=comment,
            intent=intent,
            reply=(data.get("reply") or "").strip(),
            auto_send_ok=not requires_human,
            reason=(data.get("reason") or "").strip(),
            offered_price=offered,
            price_verdict=verdict,
            source="llm",
        )

    # ── テンプレート経由 ────────────────────────────────────
    def _respond_with_template(
        self,
        comment: str,
        product: Product,
        intent: CommentIntent,
        offered: int | None,
        verdict: dict[str, Any],
        shipping_label: str,
    ) -> CommentReply:
        if intent == CommentIntent.PRICE_NEGOTIATION:
            reply = self._price_template(offered, verdict)
        else:
            defects_note = (
                f"なお、{product.defects[0]}がございます。" if product.defects else ""
            )
            reply = _TEMPLATES[intent].format(
                shipping_label=shipping_label or "匿名配送",
                condition_label=CONDITIONS.get(product.condition, product.condition),
                defects_note=defects_note,
            )

        auto_ok = intent in (
            CommentIntent.STOCK_CHECK,
            CommentIntent.SHIPPING,
            CommentIntent.CONDITION_QUESTION,
        )
        return CommentReply(
            comment=comment,
            intent=intent,
            reply=reply,
            auto_send_ok=auto_ok,
            reason="テンプレートから生成（Claude API 未設定または呼び出し失敗）",
            offered_price=offered,
            price_verdict=verdict,
            source="template",
        )

    @staticmethod
    def _price_template(offered: int | None, verdict: dict[str, Any]) -> str:
        kind = str(verdict.get("verdict", ""))
        floor = int(verdict.get("floor_price", 0) or 0)
        if kind == "accept" and offered:
            return (
                "コメントありがとうございます。\n"
                f"{offered:,}円にお値下げいたします。専用にはいたしませんので、"
                "変更後そのままご購入ください。"
            )
        if kind == "counter" and floor:
            return (
                "コメントありがとうございます。\n"
                f"恐れ入りますが、ご提示の金額は難しく、{floor:,}円まででしたらお値下げ可能です。\n"
                "ご検討いただけますと幸いです。"
            )
        if kind == "decline" and floor:
            return (
                "コメントありがとうございます。\n"
                f"申し訳ございませんが、ご提示の金額へのお値下げは難しい状況です。\n"
                f"{floor:,}円が限界となりますので、ご検討いただけますと幸いです。"
            )
        return (
            "コメントありがとうございます。\n"
            "お値下げのご相談を承ります。ご希望の金額をお知らせいただけますでしょうか。"
        )
