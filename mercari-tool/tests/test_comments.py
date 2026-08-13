"""コメント返信のテスト。

Claude API を呼ばずに動く経路（意図分類・金額抽出・テンプレート）を検証する。
金額の判断がモデルではなく価格エンジン側で決まっていることを確かめるのが要点。
"""

from __future__ import annotations

import pytest

from mercari_tool.content import CommentIntent, CommentResponder, classify_intent
from mercari_tool.content.comments import extract_offered_price


# ── 意図分類 ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "comment,expected",
    [
        ("お値下げ可能でしょうか？", CommentIntent.PRICE_NEGOTIATION),
        ("3000円になりませんか", CommentIntent.PRICE_NEGOTIATION),
        ("即決で購入したいです", CommentIntent.PRICE_NEGOTIATION),
        ("まだ在庫はありますか？", CommentIntent.STOCK_CHECK),
        ("購入可能ですか", CommentIntent.STOCK_CHECK),
        ("発送はいつになりますか", CommentIntent.SHIPPING),
        ("匿名配送でお願いできますか", CommentIntent.SHIPPING),
        ("傷や汚れはありますか", CommentIntent.CONDITION_QUESTION),
        ("サイズは何cmですか", CommentIntent.CONDITION_QUESTION),
        ("専用にしていただけますか", CommentIntent.RESERVE_REQUEST),
        ("商品が届きません", CommentIntent.COMPLAINT),
        ("説明と違う商品が届きました", CommentIntent.COMPLAINT),
        ("こんにちは", CommentIntent.OTHER),
    ],
)
def test_classify_intent(comment, expected):
    assert classify_intent(comment) is expected


def test_complaint_wins_over_price_when_both_present():
    """「返品したいので返金を」は値下げではなくクレームとして扱う。"""
    assert (
        classify_intent("値下げしてもらいましたが破損していたので返金してください")
        is CommentIntent.COMPLAINT
    )


# ── 金額抽出 ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "comment,expected",
    [
        ("3000円になりませんか", 3000),
        ("3,500円でいかがでしょう", 3500),
        ("1.5万円まででお願いします", 15000),
        ("2万でどうですか", 20000),
        ("お値下げ可能ですか", None),
    ],
)
def test_extract_offered_price(comment, expected):
    assert extract_offered_price(comment) == expected


def test_extract_offered_price_takes_the_lowest_when_several():
    """複数出てきたら、相手の希望額とみなして低いほうを採る。"""
    assert extract_offered_price("5000円を4000円にしてください") == 4000


# ── テンプレート経路 ────────────────────────────────────────
@pytest.fixture
def responder(engine) -> CommentResponder:
    # llm を渡さないので必ずテンプレート経路になる
    return CommentResponder(llm=None, pricing=engine)


def test_stock_reply_is_safe_to_auto_send(responder, product):
    reply = responder.respond("在庫ありますか？", product, 5000)
    assert reply.intent is CommentIntent.STOCK_CHECK
    assert reply.auto_send_ok is True
    assert reply.source == "template"
    assert "在庫" in reply.reply


def test_complaint_reply_always_requires_human_review(responder, product):
    reply = responder.respond("商品が破損して届きました", product, 5000)
    assert reply.intent is CommentIntent.COMPLAINT
    assert reply.auto_send_ok is False


def test_price_negotiation_reply_never_auto_sends(responder, product):
    reply = responder.respond("4000円になりませんか", product, 5000)
    assert reply.auto_send_ok is False


def test_acceptable_offer_is_accepted_with_the_offered_amount(responder, product):
    reply = responder.respond("4500円になりませんか", product, 5000)
    assert reply.price_verdict["verdict"] == "accept"
    assert "4,500円" in reply.reply


def test_unprofitable_offer_is_declined_and_quotes_the_floor(responder, product):
    reply = responder.respond("500円になりませんか", product, 5000)
    assert reply.price_verdict["verdict"] == "decline"
    floor = reply.price_verdict["floor_price"]
    assert f"{floor:,}円" in reply.reply
    # 提示された赤字価格を返信文に書いてしまわないこと
    assert "500円にお値下げ" not in reply.reply


def test_negotiation_without_an_amount_asks_back(responder, product):
    reply = responder.respond("お値下げ可能でしょうか", product, 5000)
    assert reply.offered_price is None
    assert reply.price_verdict["verdict"] == "ask_amount"
    assert "希望" in reply.reply or "金額" in reply.reply


def test_condition_reply_mentions_declared_defects(responder, product):
    reply = responder.respond("傷はありますか", product, 5000)
    assert "角に小傷あり" in reply.reply


def test_respond_many_handles_a_batch(responder, product):
    replies = responder.respond_many(
        ["在庫ありますか", "発送はいつ？", "3000円希望です"], product, 5000
    )
    assert len(replies) == 3
    assert [r.intent for r in replies] == [
        CommentIntent.STOCK_CHECK,
        CommentIntent.SHIPPING,
        CommentIntent.PRICE_NEGOTIATION,
    ]


def test_reply_is_serialisable(responder, product):
    reply = responder.respond("在庫ありますか", product, 5000)
    data = reply.to_dict()
    assert data["intent"] == "stock_check"
    assert data["intent_label"] == "在庫確認"
