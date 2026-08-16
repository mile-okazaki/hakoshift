"""Claude API のラッパー。

文章生成（タイトル・説明文・コメント返信）はすべてここを経由する。
構造化出力を使い、モデルの返答を必ず決まった JSON 形に落とす。
"""

from __future__ import annotations

import importlib.util
import json
from typing import Any

from .config import Config


class LLMError(RuntimeError):
    """API 呼び出しに関する失敗。"""


class LLMRefusal(LLMError):
    """安全性の判定でモデルが応答を拒否した。"""


class LLMClient:
    """Claude を呼ぶ薄いラッパー。

    anthropic パッケージは遅延インポートする。価格計算や売上集計だけを
    使いたい利用者に API キーやSDKを必須にしないため。
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Any = None

    # ── 接続 ──────────────────────────────────────────────
    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "anthropic パッケージが必要です: pip install anthropic"
                ) from exc
            if not self.config.anthropic_api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY が未設定です。.env に設定してください。\n"
                    "取得先: https://console.anthropic.com/"
                )
            self._client = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        return self._client

    def is_available(self) -> bool:
        """API を呼べる状態かどうか（呼び出しは行わない）。"""
        if not self.config.anthropic_api_key:
            return False
        return importlib.util.find_spec("anthropic") is not None

    # ── 呼び出し ──────────────────────────────────────────
    def _create(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        output_format: dict[str, Any] | None,
        effort: str | None,
    ) -> Any:
        output_config: dict[str, Any] = {"effort": effort or self.config.effort}
        if output_format is not None:
            output_config["format"] = output_format

        try:
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=max_tokens,
                system=system,
                output_config=output_config,
                messages=[{"role": "user", "content": user}],
            )
        except LLMError:
            raise
        except Exception as exc:
            # 認証エラー・接続エラー・レート制限など、anthropic 側の例外を
            # すべて LLMError に包む。呼び出し側は LLMError を合図に
            # ルールベースの組み立てへ切り替える設計のため、生の例外を
            # 通すと「APIが使えないときはテンプレートに切り替わる」という
            # 約束が破れてクラッシュする。
            raise LLMError(f"Claude API の呼び出しに失敗しました: {exc}") from exc

        # Claude Opus 5 は安全性判定で応答を拒否することがある。
        # content を読む前に必ず stop_reason を確認する。
        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f"（分類: {response.stop_details.category}）"
            raise LLMRefusal(f"モデルが応答を拒否しました{detail}。入力内容を見直してください。")
        return response

    @staticmethod
    def _check_truncation(response: Any) -> None:
        """max_tokens で切られた応答を、壊れたまま返さない。

        Claude Opus 5 は既定で思考しており、max_tokens は思考と本文の合計に
        かかる。枠が小さいと本文が途中で切れるため、黙って通さず失敗させる。
        """
        if response.stop_reason == "max_tokens":
            raise LLMError(
                "出力が max_tokens で打ち切られました。"
                "max_tokens を増やすか、effort を下げてください。"
            )

    @staticmethod
    def _first_text(response: Any) -> str:
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    def complete_text(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4000,
        effort: str | None = None,
    ) -> str:
        """自由記述のテキストを1つ返す。"""
        response = self._create(
            system, user, max_tokens=max_tokens, output_format=None, effort=effort
        )
        self._check_truncation(response)
        return self._first_text(response).strip()

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        max_tokens: int = 8000,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """JSON Schema に従う辞書を返す。

        構造化出力を使うので、返答が壊れて parse に失敗することは基本的に無い。
        """
        response = self._create(
            system,
            user,
            max_tokens=max_tokens,
            output_format={"type": "json_schema", "schema": schema},
            effort=effort,
        )
        self._check_truncation(response)
        text = self._first_text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover - 構造化出力では稀
            raise LLMError(f"JSON の解析に失敗しました: {text[:200]}") from exc
