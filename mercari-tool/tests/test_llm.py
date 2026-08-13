"""Claude API 呼び出しのテスト。

実際の API は叩かず、SDK クライアントを差し替えて
「どんなリクエストを送っているか」「異常な応答をどう扱うか」を検証する。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mercari_tool.config import Config
from mercari_tool.content import ListingCopyGenerator
from mercari_tool.content.comments import CommentIntent, CommentResponder
from mercari_tool.llm import LLMClient, LLMError, LLMRefusal
from mercari_tool.models import Product


class FakeMessages:
    """anthropic の client.messages を模したスタブ。"""

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response(kwargs) if callable(self.response) else self.response


def _text_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
    )


def _client_with(response) -> tuple[LLMClient, FakeMessages]:
    config = Config(anthropic_api_key="test-key", model="claude-opus-5", effort="medium")
    llm = LLMClient(config)
    messages = FakeMessages(response)
    llm._client = SimpleNamespace(messages=messages)
    return llm, messages


# ── リクエストの形 ──────────────────────────────────────────
def test_request_uses_configured_model_and_effort():
    llm, messages = _client_with(_text_response("こんにちは"))
    llm.complete_text("システム", "ユーザー")

    call = messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["system"] == "システム"
    assert call["messages"] == [{"role": "user", "content": "ユーザー"}]
    assert call["output_config"]["effort"] == "medium"


def test_effort_can_be_overridden_per_call():
    llm, messages = _client_with(_text_response("ok"))
    llm.complete_text("s", "u", effort="low")
    assert messages.calls[0]["output_config"]["effort"] == "low"


def test_request_never_sends_removed_sampling_parameters():
    """temperature / top_p / top_k は Claude Opus 5 では 400 になる。"""
    llm, messages = _client_with(_text_response("ok"))
    llm.complete_text("s", "u")
    call = messages.calls[0]
    for removed in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert removed not in call


def test_json_request_attaches_the_schema():
    schema = {"type": "object", "properties": {"a": {"type": "string"}},
              "required": ["a"], "additionalProperties": False}
    llm, messages = _client_with(_text_response('{"a": "b"}'))
    result = llm.complete_json("s", "u", schema)

    fmt = messages.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is schema
    assert result == {"a": "b"}


# ── 異常応答の扱い ──────────────────────────────────────────
def test_refusal_raises_before_reading_content():
    """拒否応答では content が空なので、読む前に落とす。"""
    refusal = SimpleNamespace(
        content=[],
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber", explanation=""),
    )
    llm, _ = _client_with(refusal)
    with pytest.raises(LLMRefusal, match="拒否"):
        llm.complete_text("s", "u")


def test_truncated_output_is_not_returned_as_if_complete():
    """max_tokens で切られた応答を、正常な結果として返さない。"""
    llm, _ = _client_with(_text_response("途中まで", stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="max_tokens"):
        llm.complete_text("s", "u")

    llm, _ = _client_with(_text_response('{"a":', stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="max_tokens"):
        llm.complete_json("s", "u", {"type": "object"})


def test_broken_json_raises_a_readable_error():
    llm, _ = _client_with(_text_response("これはJSONではない"))
    with pytest.raises(LLMError, match="JSON"):
        llm.complete_json("s", "u", {"type": "object"})


def test_missing_api_key_is_reported_clearly():
    llm = LLMClient(Config(anthropic_api_key=None))
    assert llm.is_available() is False
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        llm.complete_text("s", "u")


# ── 生成結果の検証 ──────────────────────────────────────────
def test_over_long_title_is_flagged_not_silently_accepted():
    long_title = "あ" * 45
    payload = json.dumps(
        {
            "titles": [
                {"text": long_title, "reason": "長すぎる案", "keywords_used": []},
                {"text": "短いタイトル", "reason": "適正", "keywords_used": ["短い"]},
            ],
            "catchphrases": ["キャッチ"],
        }
    )
    llm, _ = _client_with(_text_response(payload))
    candidates, catchphrases = ListingCopyGenerator(llm).generate_titles(
        Product(sku="X", name="テスト")
    )
    # 上限内の案が先頭に来て、超過案には警告が付く
    assert candidates[0].text == "短いタイトル"
    over = next(c for c in candidates if c.text == long_title)
    assert not over.is_valid
    assert any("40文字を超え" in w for w in over.warnings)
    assert catchphrases == ["キャッチ"]


def test_risky_phrases_are_flagged(product):
    payload = json.dumps(
        {
            "description": "【状態】角に小傷ありますが良品です。正規品保証いたします。",
            "hashtags": ["#テスト"],
            "highlights": ["状態"],
        }
    )
    llm, _ = _client_with(_text_response(payload))
    result = ListingCopyGenerator(llm).generate_description(product)
    assert any("正規品保証" in w for w in result.warnings)
    assert result.hashtags == ["テスト"]  # # は取り除かれる


def test_undisclosed_defect_is_flagged(product):
    """申告した難点が説明文に無い場合、取引トラブルの元になるので警告する。"""
    payload = json.dumps(
        {"description": "【状態】とてもきれいです。", "hashtags": [], "highlights": []}
    )
    llm, _ = _client_with(_text_response(payload))
    result = ListingCopyGenerator(llm).generate_description(product)
    assert any("角に小傷あり" in w and "反映されていません" in w for w in result.warnings)


# ── コメント返信 ────────────────────────────────────────────
def test_comment_prompt_carries_the_price_verdict(engine, product):
    """値下げの可否は価格エンジンが決め、その結論をプロンプトに明示する。"""
    payload = json.dumps(
        {"reply": "4,500円にお値下げします。", "intent": "price_negotiation",
         "requires_human": False, "reason": ""}
    )
    llm, messages = _client_with(_text_response(payload))
    reply = CommentResponder(llm=llm, pricing=engine).respond(
        "4500円になりませんか", product, 5000
    )

    prompt = messages.calls[0]["messages"][0]["content"]
    assert "受諾する" in prompt
    assert "これ以外の金額を返信文に書かないでください" in prompt
    # モデルが false と言っても、値下げは人の確認を必須にする
    assert reply.auto_send_ok is False
    assert reply.source == "llm"


def test_complaint_always_requires_human_even_if_model_says_otherwise(engine, product):
    payload = json.dumps(
        {"reply": "確認します。", "intent": "complaint", "requires_human": False, "reason": ""}
    )
    llm, _ = _client_with(_text_response(payload))
    reply = CommentResponder(llm=llm, pricing=engine).respond(
        "商品が破損していました", product, 5000
    )
    assert reply.intent is CommentIntent.COMPLAINT
    assert reply.auto_send_ok is False


def test_api_failure_falls_back_to_template(engine, product):
    def boom(_kwargs):
        raise LLMError("API がダウンしています")

    llm, _ = _client_with(boom)
    reply = CommentResponder(llm=llm, pricing=engine).respond(
        "在庫ありますか", product, 5000
    )
    assert reply.source == "template"
    assert "在庫" in reply.reply


# ── APIキー無しでのフォールバック説明文 ──────────────────────
def test_fallback_description_includes_declared_defects(product):
    """API が使えなくても、申告済みの難点は必ず説明文に載る。"""
    from mercari_tool.content import build_fallback_description

    result = build_fallback_description(product, shipping_label="ネコポス")
    assert "角に小傷あり" in result.description
    assert "目立った傷や汚れなし" in result.description
    assert "ネコポス" in result.description
    assert "【商品の詳細】" in result.description
    assert "【状態】" in result.description


def test_fallback_description_uses_keywords_as_hashtags(product):
    from mercari_tool.content import build_fallback_description

    result = build_fallback_description(product)
    assert "TestBrand" in result.hashtags
    assert "バッグ" in result.hashtags


def test_fallback_description_states_no_known_defects_when_none(product):
    from mercari_tool.content import build_fallback_description

    product.defects = []
    result = build_fallback_description(product)
    assert "見当たりません" in result.description
