"""Claude を使わないタイトル・キャッチコピーの組み立て。

APIキーが無くても「投稿の手前まで」が完結するように、
商品マスタと相場データだけでタイトルを作る。

検索で見つかるかどうかはタイトルの語順でほぼ決まる。
メルカリの検索は入力語をすべて含む出品を返すため、
「買い手が打ちそうな語」を漏らさず、かつ40文字に収めるのが要件になる。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..models import MarketStats, Product
from .generator import TitleCandidate
from .prompts import TITLE_MAX_CHARS

#: 状態を表す語をタイトルに入れてよい条件。
#: 「美品」「新品」は買い手が検索する語だが、実態と違うと通報対象になる。
_CONDITION_WORD = {
    "new": "新品未使用",
    "like_new": "未使用に近い",
    "no_scratch": "美品",
}

#: タイトルに入れても検索の役に立たない語
_NOISE = re.compile(r"^(送料無料|送料込み?|即決|お買い得|激安|訳あり)$")


def _normalize(text: str) -> str:
    """全角英数を半角に寄せ、重複判定をしやすくする。"""
    return unicodedata.normalize("NFKC", text).strip().lower()


def _contains(haystack: str, needle: str) -> bool:
    """既にタイトルに含まれている語かどうか。"""
    if not needle:
        return True
    return _normalize(needle) in _normalize(haystack)


@dataclass(frozen=True)
class _Segment:
    """タイトルを構成する語のひとかたまり。"""

    text: str
    #: 小さいほど重要。40文字に収まらないとき、大きいものから落とす。
    priority: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", self.text.strip())


def _dedupe_tokens(text: str) -> str:
    """同じ語が2回出てくるタイトルを整える。

    「27cm」がサイズ欄と相場キーワードの両方から入る、といった重なりを
    最後にまとめて落とす。検索上も見た目上も、重複に意味はない。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for token in text.split():
        key = _normalize(token)
        if key in seen:
            continue
        seen.add(key)
        kept.append(token)
    return " ".join(kept)


def _assemble(segments: list[_Segment], limit: int = TITLE_MAX_CHARS) -> str:
    """重要な順に詰めて、上限に収まるタイトルを作る。

    語順は渡された並びを保つ。落とすのは優先度の低いものから。
    同じ語が2回現れても取り違えないよう、並び順は添字で管理する。
    """
    # (元の位置, セグメント) にしてから優先度順に試す
    indexed = [(i, s) for i, s in enumerate(segments) if s.text]
    chosen: list[tuple[int, _Segment]] = []
    for entry in sorted(indexed, key=lambda pair: pair[1].priority):
        trial = sorted(chosen + [entry], key=lambda pair: pair[0])
        if len(" ".join(s.text for _, s in trial)) <= limit:
            chosen = trial
    return _dedupe_tokens(
        " ".join(s.text for _, s in sorted(chosen, key=lambda pair: pair[0]))
    )


def _condition_word_for(product: Product) -> str:
    """タイトルに入れてよい状態の語を返す。無ければ空文字。

    申告した難点と矛盾する語は使わない。傷を正直に書いておきながら
    タイトルに「新品未使用」と載せるのは、規約違反であり評価も落とす。

    - 新品 / 未使用に近い … 難点が1つでもあれば矛盾するので使わない
    - 美品（目立った傷や汚れなし）… 軽微な難点1つまでは許容。
      2つ以上あるなら「美品」は言い過ぎなので外す。
    """
    word = _CONDITION_WORD.get(product.condition, "")
    if not word:
        return ""
    defect_count = len([d for d in product.defects if d.strip()])
    if product.condition in ("new", "like_new") and defect_count > 0:
        return ""
    if product.condition == "no_scratch" and defect_count >= 2:
        return ""
    return word


def _applicable_keywords(product: Product, market: MarketStats | None) -> list[str]:
    """相場で頻出していて、かつこの商品に事実として当てはまる語。

    「売れているタイトルに多いから」だけで語を足すと、
    実態と違うキーワードを付けることになる。商品側の登録内容
    （商品名・ブランド・キーワード・サイズ）に裏付けがある語だけを通す。
    """
    if not market or not market.hot_keywords:
        return []
    backing = " ".join(
        [product.name, product.brand, product.size_note, *product.keywords]
    )
    return [
        word
        for word in market.hot_keywords
        if _contains(backing, word) and not _NOISE.match(word)
    ]


def build_titles(
    product: Product,
    market: MarketStats | None = None,
    limit: int = 5,
) -> tuple[list[TitleCandidate], list[str]]:
    """タイトル候補とキャッチコピーを、生成AIを使わずに作る。

    Returns:
        (タイトル候補, キャッチコピー)
    """
    brand = product.brand.strip()
    name = product.name.strip()
    size = product.size_note.strip()
    condition_word = _condition_word_for(product)
    hot = _applicable_keywords(product, market)
    # 商品名やブランドに既に出ている語を重ねない
    extra_keywords = [
        k.strip()
        for k in product.keywords
        if k.strip() and not _contains(f"{name} {brand}", k) and not _NOISE.match(k.strip())
    ]
    hot_extra = [k for k in hot if not _contains(f"{name} {brand}", k)]

    # ブランドが商品名の中に既にある場合は前置しない
    brand_segment = "" if (not brand or _contains(name, brand)) else brand
    size_segment = "" if (not size or _contains(name, size)) else size

    # 既にタイトルへ入る予定の語は、キーワードとして重ねて足さない
    covered = f"{name} {brand} {size}"
    extra_keywords = [k for k in extra_keywords if not _contains(covered, k)]
    hot_extra = [k for k in hot_extra if not _contains(covered, k)]

    def variant(segments: list[_Segment], reason: str) -> TitleCandidate | None:
        text = _assemble(segments)
        if not text:
            return None
        return TitleCandidate(
            text=text,
            reason=reason,
            keywords_used=[s.text for s in segments if s.text and s.text in text],
        )

    plans: list[tuple[list[_Segment], str]] = []

    # A: 王道。ブランド → 商品名 → サイズ → 状態
    plans.append((
        [
            _Segment(brand_segment, 1),
            _Segment(name, 0),
            _Segment(size_segment, 2),
            _Segment(condition_word, 4),
        ],
        "ブランド・商品名・サイズの順。もっとも一般的な検索のされ方に合わせた形。",
    ))

    # B: 相場で頻出している語を優先して入れる
    if hot_extra:
        plans.append((
            [
                _Segment(brand_segment, 1),
                _Segment(name, 0),
                _Segment(" ".join(hot_extra[:2]), 2),
                _Segment(size_segment, 3),
            ],
            f"売れている出品に多い語「{' / '.join(hot_extra[:2])}」を前寄りに配置。",
        ))

    # C: 登録キーワードを足して検索の入口を増やす
    if extra_keywords:
        plans.append((
            [
                _Segment(brand_segment, 1),
                _Segment(name, 0),
                _Segment(" ".join(extra_keywords[:2]), 2),
                _Segment(condition_word, 4),
            ],
            f"登録キーワード「{' / '.join(extra_keywords[:2])}」で検索の入口を増やす形。",
        ))

    # D: 状態を先頭に出す（美品・新品を探している層に当てる）
    if condition_word:
        plans.append((
            [
                _Segment(condition_word, 2),
                _Segment(brand_segment, 1),
                _Segment(name, 0),
                _Segment(size_segment, 3),
            ],
            f"「{condition_word}」を先頭に置き、状態で絞り込む買い手に当てる形。",
        ))

    # E: 商品名とサイズだけの最短形。語が多すぎて読みにくいときの逃げ。
    plans.append((
        [_Segment(name, 0), _Segment(size_segment, 1)],
        "余計な語を省いた最短形。商品名がはっきりしている場合はこれで十分。",
    ))

    candidates: list[TitleCandidate] = []
    seen: set[str] = set()
    for segments, reason in plans:
        candidate = variant(segments, reason)
        if candidate is None:
            continue
        key = _normalize(candidate.text)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
        if len(candidates) >= limit:
            break

    return candidates, build_catchphrases(product)


def build_catchphrases(product: Product) -> list[str]:
    """説明文の冒頭に置く1文を、登録内容から組み立てる。

    誇張は入れない。書けるのは、商品マスタに登録された事実だけ。
    """
    condition_label = product.condition_label
    phrases: list[str] = []

    if product.selling_points:
        first = product.selling_points[0].rstrip("。")
        phrases.append(f"{first}。状態は「{condition_label}」です。")
        if len(product.selling_points) > 1:
            second = product.selling_points[1].rstrip("。")
            phrases.append(f"{first}、{second}。{product.name}をお探しの方へ。")

    if product.brand:
        phrases.append(f"{product.brand}の{product.name}です。状態は「{condition_label}」。")

    phrases.append(f"{product.name}をお譲りします。状態は「{condition_label}」です。")

    # 重複を除きつつ順序は保つ
    unique: list[str] = []
    for phrase in phrases:
        if phrase not in unique:
            unique.append(phrase)
    return unique[:3]
