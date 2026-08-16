"""タイトル・キャッチコピー・説明文の生成。

生成結果はそのまま信用せず、文字数上限や禁止表現をコード側で検証してから返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm import LLMClient
from ..models import MarketStats, Product
from .prompts import (
    COPY_SYSTEM_PROMPT,
    DESCRIPTION_MAX_CHARS,
    DESCRIPTION_SCHEMA,
    TITLE_MAX_CHARS,
    TITLE_SCHEMA,
    product_context,
)

#: 使うとトラブルになりやすい表現。生成後にチェックして警告を出す。
RISKY_PHRASES = [
    "正規品保証",
    "本物保証",
    "鑑定済み",
    "返品不可",
    "ノークレームノーリターン",
    "完全無欠",
    "効果は確実",
    "必ず痩せ",
    "副作用はありません",
]


@dataclass
class TitleCandidate:
    text: str
    reason: str = ""
    keywords_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def is_valid(self) -> bool:
        return 0 < self.length <= TITLE_MAX_CHARS and not self.warnings


@dataclass
class DescriptionResult:
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.description)


def _check_risky(text: str) -> list[str]:
    return [f"リスクのある表現が含まれています: 「{p}」" for p in RISKY_PHRASES if p in text]


class ListingCopyGenerator:
    """出品用コピーを作る。"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    # ── タイトル・キャッチコピー ────────────────────────────
    def generate_titles(
        self,
        product: Product,
        market: MarketStats | None = None,
        count: int = 5,
    ) -> tuple[list[TitleCandidate], list[str]]:
        """タイトル候補とキャッチコピーを生成する。

        Returns:
            (タイトル候補, キャッチコピー)
        """
        user = f"""{product_context(product, market)}

## 依頼
この商品のメルカリ出品タイトルを {count} 案と、
商品説明の冒頭に置くキャッチコピーを3案つくってください。

タイトルの条件:
- 全角{TITLE_MAX_CHARS}文字以内（厳守）
- 購入者が実際に検索する語を前半に置く
- 「ブランド名 + 商品名 + 型番/サイズ/色 + 状態」の順を基本とする
- 案ごとに切り口を変える（型番重視 / 用途重視 / 状態重視 など）
- reason には「なぜこの語順・語彙にしたか」を1文で書く

キャッチコピーの条件:
- 40〜60文字程度の1文
- 誇張せず、この商品を買う理由が伝わるもの
"""
        data = self.llm.complete_json(
            COPY_SYSTEM_PROMPT, user, TITLE_SCHEMA, max_tokens=8000
        )

        candidates: list[TitleCandidate] = []
        for row in data.get("titles", []):
            text = (row.get("text") or "").strip()
            warnings = _check_risky(text)
            if len(text) > TITLE_MAX_CHARS:
                warnings.append(
                    f"{TITLE_MAX_CHARS}文字を超えています（{len(text)}文字）。短縮が必要です。"
                )
            candidates.append(
                TitleCandidate(
                    text=text,
                    reason=(row.get("reason") or "").strip(),
                    keywords_used=list(row.get("keywords_used") or []),
                    warnings=warnings,
                )
            )

        # 上限内に収まっているものを先頭に寄せる
        candidates.sort(key=lambda c: (not c.is_valid, c.length))
        catchphrases = [c.strip() for c in data.get("catchphrases", []) if c.strip()]
        return candidates, catchphrases

    # ── 説明文 ─────────────────────────────────────────────
    def generate_description(
        self,
        product: Product,
        market: MarketStats | None = None,
        catchphrase: str = "",
        shipping_label: str = "",
    ) -> DescriptionResult:
        """商品説明文を生成する。"""
        extra = []
        if catchphrase:
            extra.append(f"- 冒頭に使うキャッチコピー: {catchphrase}")
        if shipping_label:
            extra.append(f"- 配送方法: {shipping_label}")
        extra_block = "\n".join(extra)

        user = f"""{product_context(product, market)}
{extra_block}

## 依頼
メルカリの商品説明文を1本つくってください。

構成:
1. 冒頭1〜2文で、この商品を買う理由を伝える
2. 「商品の詳細」— ブランド / 型番 / サイズ / 色 / 付属品 を箇条書き
3. 「状態」— 良い点と難点の両方を具体的に。難点は必ず全部書く
4. 「発送について」— 配送方法と、発送までの目安
5. 「ご購入前に」— 中古品であることの了承など、簡潔に2〜3行

条件:
- 全体で{DESCRIPTION_MAX_CHARS}文字以内（厳守）
- 見出しは【】で囲む
- 箇条書きは「・」を使う
- 過度な絵文字や記号の装飾はしない
- hashtags には検索されやすいタグを5〜8個（#は付けずに語だけ）
- highlights には、この説明文で最も効いている訴求ポイントを3つ
"""
        data = self.llm.complete_json(
            COPY_SYSTEM_PROMPT, user, DESCRIPTION_SCHEMA, max_tokens=10000
        )

        description = (data.get("description") or "").strip()
        warnings = _check_risky(description)
        if len(description) > DESCRIPTION_MAX_CHARS:
            warnings.append(
                f"{DESCRIPTION_MAX_CHARS}文字を超えています（{len(description)}文字）。"
            )
        # 難点を申告しているのに説明文に出てこない場合は取引トラブルの元になる
        for defect in product.defects:
            head = re.split(r"[、。\s]", defect)[0]
            if head and head not in description:
                warnings.append(f"申告した難点「{defect}」が説明文に反映されていません。")

        return DescriptionResult(
            description=description,
            hashtags=[h.lstrip("#").strip() for h in data.get("hashtags", []) if h.strip()],
            highlights=[h.strip() for h in data.get("highlights", []) if h.strip()],
            warnings=warnings,
        )


def build_fallback_description(
    product: Product, shipping_label: str = ""
) -> DescriptionResult:
    """商品マスタの登録内容だけで説明文を組み立てる。

    Claude API が使えないときでも、説明欄が空のまま出品作業に入らずに済むように。
    生成AIのような読ませる文章にはならないが、必要な情報は漏れなく載る。
    """
    lines: list[str] = []
    if product.selling_points:
        lines.append(product.selling_points[0])
        lines.append("")

    lines.append("【商品の詳細】")
    if product.brand:
        lines.append(f"・ブランド：{product.brand}")
    lines.append(f"・商品名：{product.name}")
    if product.size_note:
        lines.append(f"・サイズ：{product.size_note}")
    for point in product.selling_points[1:]:
        lines.append(f"・{point}")

    lines.append("")
    lines.append("【状態】")
    lines.append(f"・{product.condition_label}")
    if product.defects:
        lines.extend(f"・{defect}" for defect in product.defects)
    else:
        lines.append("・気になる大きな傷や汚れは見当たりません。")

    lines.append("")
    lines.append("【発送について】")
    lines.append(f"・{shipping_label or '匿名配送'}でお送りします。")
    lines.append("・ご購入後、1〜2日以内に発送いたします。")

    lines.append("")
    lines.append("【ご購入前に】")
    lines.append("・中古品のため、細かな使用感はご了承ください。")
    lines.append("・気になる点は購入前にコメントでお問い合わせください。")

    if product.notes:
        lines.append(f"・{product.notes}")

    description = "\n".join(lines).strip()
    warnings: list[str] = []
    if len(description) > DESCRIPTION_MAX_CHARS:
        description = description[:DESCRIPTION_MAX_CHARS]
        warnings.append(f"{DESCRIPTION_MAX_CHARS}文字に切り詰めました。")

    hashtags = [k for k in (product.keywords or []) if k][:8]
    if product.brand and product.brand not in hashtags:
        hashtags.insert(0, product.brand)

    return DescriptionResult(
        description=description,
        hashtags=hashtags,
        highlights=list(product.selling_points[:3]),
        warnings=warnings,
    )
