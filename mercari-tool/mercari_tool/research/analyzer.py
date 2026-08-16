"""相場サンプルの集計。

外れ値（間違った値付け・別商品の混入）に引きずられないよう、
統計を取る前に IQR で刈り込んでいる。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Sequence

from ..models import CONDITIONS, MarketStats, SoldComp

# ── 日本語トークナイズ ───────────────────────────────────────
_KATAKANA = re.compile(r"[ァ-ヴー]{2,}")
_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")
_KANJI = re.compile(r"[一-鿿]{2,}")

#: 出品タイトルに頻出するが商品を特定しない語
_STOPWORDS = {
    "送料無料",
    "送料込み",
    "送料込",
    "美品",
    "新品",
    "未使用",
    "中古",
    "即決",
    "値下げ",
    "セール",
    "限定",
    "本日",
    "最安",
    "レア",
    "正規品",
    "匿名配送",
    "即日発送",
    "セット",
    "まとめ",
    "used",
    "new",
    "sale",
    "free",
}


def tokenize_ja(text: str) -> list[str]:
    """形態素解析器を使わずに、商品名から意味のありそうな語を拾う。

    カタカナ列・英数字列・漢字列を語として取り出す。完璧ではないが、
    「どのキーワードが売れている出品に多いか」を見るには十分な精度が出る。
    """
    if not text:
        return []
    tokens: list[str] = []
    tokens.extend(m.group() for m in _KATAKANA.finditer(text))
    tokens.extend(m.group() for m in _ALNUM.finditer(text))
    for match in _KANJI.finditer(text):
        run = match.group()
        if len(run) <= 4:
            tokens.append(run)
        else:
            # 長い漢字列は2文字ずつに割って部分一致を拾えるようにする
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    result = []
    for token in tokens:
        normalized = token.strip(".-").lower()
        if len(normalized) < 2:
            continue
        if normalized in _STOPWORDS:
            continue
        if normalized.isdigit():
            continue
        result.append(token.strip(".-"))
    return result


# ── 外れ値の除去 ────────────────────────────────────────────
def _trim_outliers(prices: Sequence[int]) -> tuple[list[int], int]:
    """外れ値の価格を落とす。落とした件数も返す。

    2段構え:
    1. 中央値の 1/6〜6倍 を外れる値を落とす（3件以上のとき）。
       桁間違い級の外れ値は IQR 自体を引き伸ばしてフェンスを無効化するので、
       外れ値に強い中央値を基準にした粗い網を先にかける。
       状態差による正当な価格差（新品1.15倍〜難あり0.6倍）は余裕で収まる。
    2. 残りに IQR の 1.5 倍ルール（4件以上のとき）。
    """
    values = sorted(prices)
    dropped = 0

    if len(values) >= 3:
        center = statistics.median(values)
        if center > 0:
            kept = [v for v in values if center / 6 <= v <= center * 6]
            if kept:
                dropped += len(values) - len(kept)
                values = kept

    if len(values) < 4:
        return values, dropped
    q1, q3 = statistics.quantiles(values, n=4)[0], statistics.quantiles(values, n=4)[2]
    iqr = q3 - q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [v for v in values if lower <= v <= upper]
    # 全部落ちてしまうケース（値がほぼ同一で IQR=0 など）は元に戻す
    if not kept:
        return values, dropped
    return kept, dropped + (len(values) - len(kept))


def _percentile(sorted_values: Sequence[int], ratio: float) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    index = ratio * (len(sorted_values) - 1)
    low = int(index)
    high = min(low + 1, len(sorted_values) - 1)
    weight = index - low
    return int(round(sorted_values[low] * (1 - weight) + sorted_values[high] * weight))


def _normalize_condition(raw: str) -> str:
    """表示名（"目立った傷や汚れなし"）を内部キーに寄せる。"""
    if not raw:
        return ""
    text = raw.strip()
    if text in CONDITIONS:
        return text
    for key, label in CONDITIONS.items():
        if label == text or label.replace("、", "") == text.replace("、", ""):
            return key
    return ""


# ── 集計本体 ────────────────────────────────────────────────
def analyze(comps: Sequence[SoldComp], query: str = "") -> MarketStats:
    """販売実績サンプルから相場統計を作る。

    価格統計は「売れた出品」だけを使う。出品中の価格は売り手の希望額であって
    成約価格ではないため、混ぜると相場が実態より高く出る。
    """
    stats = MarketStats(query=query, sample_size=len(comps))
    if not comps:
        stats.warnings.append("サンプルが0件です。相場データを用意してください。")
        return stats

    sold = [c for c in comps if c.sold and c.price > 0]
    active = [c for c in comps if not c.sold and c.price > 0]
    stats.sold_count = len(sold)
    stats.active_count = len(active)
    stats.sell_through_rate = len(sold) / len(comps) if comps else 0.0

    basis = sold or [c for c in comps if c.price > 0]
    if not basis:
        stats.warnings.append("有効な価格を持つサンプルがありません。")
        return stats
    if not sold:
        stats.warnings.append(
            "売却済みサンプルが0件のため、出品中の価格で代用しています。"
            "実際の成約価格より高く出ている可能性があります。"
        )

    prices, trimmed = _trim_outliers([c.price for c in basis])
    if trimmed:
        stats.warnings.append(f"外れ値 {trimmed} 件を統計から除外しました。")

    stats.median_price = int(statistics.median(prices))
    stats.mean_price = int(statistics.fmean(prices))
    stats.p25_price = _percentile(prices, 0.25)
    stats.p75_price = _percentile(prices, 0.75)
    stats.min_price = int(min(prices))
    stats.max_price = int(max(prices))

    # 売れるまでの日数
    durations = [c.days_to_sell for c in sold if c.days_to_sell is not None]
    if durations:
        stats.avg_days_to_sell = round(statistics.fmean(durations), 1)

    # 価格トレンド: 売却日が分かるサンプルを前半・後半に割って中央値を比べる
    dated = sorted([c for c in sold if c.sold_at], key=lambda c: c.sold_at)  # type: ignore[arg-type]
    if len(dated) >= 6:
        half = len(dated) // 2
        older = statistics.median([c.price for c in dated[:half]])
        recent = statistics.median([c.price for c in dated[half:]])
        if older > 0:
            stats.price_trend = round(recent / older, 3)

    # 状態別の中央値
    by_condition: dict[str, list[int]] = {}
    for comp in basis:
        key = _normalize_condition(comp.condition)
        if key:
            by_condition.setdefault(key, []).append(comp.price)
    stats.median_by_condition = {
        key: int(statistics.median(values))
        for key, values in by_condition.items()
        if len(values) >= 2  # 1件だけの状態は中央値として扱わない
    }

    # 売れた出品タイトルに多い語
    counter: Counter[str] = Counter()
    for comp in sold or basis:
        counter.update(set(tokenize_ja(comp.title)))
    threshold = max(2, len(sold or basis) // 5)
    stats.hot_keywords = [
        word for word, count in counter.most_common(20) if count >= threshold
    ][:12]

    # 判断材料としての注意書き
    if stats.sample_size < 5:
        stats.warnings.append("サンプルが5件未満です。相場の確度は低いと考えてください。")
    if stats.max_price and stats.min_price and stats.max_price > stats.min_price * 3:
        stats.warnings.append(
            "価格帯が3倍以上に広がっています。別商品が混ざっていないか確認してください。"
        )
    if stats.price_trend is not None:
        if stats.price_trend >= 1.1:
            stats.warnings.append(
                f"直近の成約価格が {(stats.price_trend - 1) * 100:.0f}% 上昇傾向です。強気の価格設定が通りやすい状況。"
            )
        elif stats.price_trend <= 0.9:
            stats.warnings.append(
                f"直近の成約価格が {(1 - stats.price_trend) * 100:.0f}% 下落傾向です。早めの売却を検討してください。"
            )

    return stats


def format_stats(stats: MarketStats) -> str:
    """人が読む用のテキストに整形する。"""
    if not stats.has_data:
        return "相場データがありません。"
    lines = [
        f"■ 相場リサーチ: {stats.query or '(クエリ未指定)'}",
        f"  サンプル       : {stats.sample_size} 件（売却済み {stats.sold_count} / 出品中 {stats.active_count}）",
        f"  売却率         : {stats.sell_through_rate:.0%}",
        f"  中央値         : {stats.median_price:,} 円",
        f"  平均           : {stats.mean_price:,} 円",
        f"  価格帯(25-75%) : {stats.p25_price:,} 〜 {stats.p75_price:,} 円",
        f"  最安 / 最高    : {stats.min_price:,} / {stats.max_price:,} 円",
    ]
    if stats.avg_days_to_sell is not None:
        lines.append(f"  平均売却日数   : {stats.avg_days_to_sell} 日")
    if stats.price_trend is not None:
        arrow = "↑" if stats.price_trend > 1.02 else ("↓" if stats.price_trend < 0.98 else "→")
        lines.append(f"  価格トレンド   : {arrow} {stats.price_trend:.2f}（直近/過去）")
    if stats.median_by_condition:
        parts = ", ".join(
            f"{CONDITIONS.get(k, k)}={v:,}円" for k, v in stats.median_by_condition.items()
        )
        lines.append(f"  状態別中央値   : {parts}")
    if stats.hot_keywords:
        lines.append(f"  頻出キーワード : {' / '.join(stats.hot_keywords)}")
    for warning in stats.warnings:
        lines.append(f"  ⚠ {warning}")
    return "\n".join(lines)
