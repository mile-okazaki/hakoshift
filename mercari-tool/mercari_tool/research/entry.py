"""相場サンプルの手入力を受け取る。

相場データを CSV で手作りするのは、日々の運用でいちばん面倒な作業になる。
ここでは「メルカリの検索結果を見ながら、値段を並べて打つ／貼る」だけで
サンプルが溜まるようにする。

想定する入力は次のような雑な行。1行1件として読む::

    9,800円 売切
    エアマックス 90 27cm 11500 SOLD
    8900
    ナイキ エアマックス 90 美品 12800円 2026-08-01

自動でメルカリを巡回して集めることはしない。検索結果の取得は
利用規約の禁止事項（自動アクセス）に当たるため、あくまで
「人が見た結果を書き写す」ための入口として用意している。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from ..models import CONDITIONS, SoldComp
from .providers import CsvFileProvider, slugify

#: 行のどこかにあれば「売れた」と読む語。
#: 「済」単独は「決済」などに当たってしまうので入れない。
_SOLD_MARKERS = re.compile(
    r"(売り切れ|売切れ|売切|売却済み?|完売|\bSOLD\s*OUT\b|\bSOLD\b)", re.IGNORECASE
)
#: 行のどこかにあれば「出品中（未売却）」と読む語
_ACTIVE_MARKERS = re.compile(
    r"(出品中|販売中|未売却|\bACTIVE\b|\bON\s*SALE\b)", re.IGNORECASE
)

#: 日付らしき並び。2026-08-01 / 2026/8/1 / 8月1日 を拾う
_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<m>\d{1,2})[/月](?P<d>\d{1,2})日"),
)

#: メルカリの最低出品価格。これ未満の「価格」は読み違いとみなす。
#: 300円未満の成約実績はメルカリには存在しないので、誤読の安全網になる。
MIN_COMP_PRICE = 300

# 数字の前後判定は ASCII の英数字だけで行う。Python の \w は漢字・カナも
# 含むため、\w を使うと「ナイキ9800円」のような日本語直結の価格が
# 全部弾かれてしまう。ASCII 限定なら型番（990v6）やサイズ（27cm, 27.5cm）
# だけを除外できる。
#: 「9,800円」のように単位が付いた金額
_PRICE_WITH_UNIT = re.compile(r"(?<![0-9A-Za-z.])(\d[\d,]*)\s*円")
#: 「1.2万円」「1万2000円」「9万円」「1.5万」のような万表記
_MAN_PRICE = re.compile(
    r"(?<![0-9A-Za-z.])(\d+(?:\.\d+)?)\s*万\s*([\d,]+)?\s*円?"
)
#: 単独で置かれた数値（型番 990v6・サイズ 27cm / 27.5cm は拾わない）
_BARE_NUMBER = re.compile(r"(?<![0-9A-Za-z.,])(\d[\d,]*)(?![0-9A-Za-z,.]*[0-9A-Za-z])")
#: 値下げ表記「12000円→9800円」の矢印
_ARROW = re.compile(r"[→⇒]")

#: 状態を表す表示名 → 内部キー。行の中に出てきたら状態として採用する。
_CONDITION_WORDS: dict[str, str] = {
    **{label: key for key, label in CONDITIONS.items()},
    "新品未使用": "new",
    "新品": "new",
    "未使用": "new",
    "未使用に近い": "like_new",
    "ほぼ新品": "like_new",
    "美品": "no_scratch",
    "目立った傷や汚れなし": "no_scratch",
    "やや傷汚れあり": "small_scratch",
    "傷汚れあり": "scratched",
    "難あり": "bad",
    "ジャンク": "bad",
}
#: 長い語から先に当てないと「新品未使用」が「新品」で切れる
_CONDITION_ORDER = sorted(_CONDITION_WORDS, key=len, reverse=True)


@dataclass
class ParsedLine:
    """1行を読んだ結果。読めなかった行も理由付きで残す。"""

    raw: str
    comp: SoldComp | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.comp is not None


def _to_int(text: str) -> int:
    return int(text.replace(",", ""))


def _extract_date(text: str) -> tuple[date | None, str]:
    """日付を1つ取り出し、残りの文字列と一緒に返す。"""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups["y"]) if groups.get("y") else date.today().year
        try:
            found = date(year, int(groups["m"]), int(groups["d"]))
        except ValueError:
            continue
        return found, (text[: match.start()] + " " + text[match.end() :])
    return None, text


def _extract_condition(text: str) -> tuple[str, str]:
    """状態の語を1つ取り出し、残りの文字列と一緒に返す。"""
    for word in _CONDITION_ORDER:
        index = text.find(word)
        if index >= 0:
            rest = text[:index] + " " + text[index + len(word) :]
            return _CONDITION_WORDS[word], rest
    return "", text


def _try_unit_price(text: str) -> tuple[int, str]:
    """単位の明示された価格（万表記・円付き）を探す。無ければ (0, 元の文字列)。"""
    match = _MAN_PRICE.search(text)
    if match:
        price = int(float(match.group(1)) * 10000)
        if match.group(2):
            price += _to_int(match.group(2))
        return price, text[: match.start()] + " " + text[match.end() :]
    match = _PRICE_WITH_UNIT.search(text)
    if match:
        return _to_int(match.group(1)), text[: match.start()] + " " + text[match.end() :]
    return 0, text


def _extract_price(text: str) -> tuple[int, str]:
    """価格を取り出し、残りの文字列と一緒に返す。

    優先順位は 万表記・「円」付き → 裸の数値。裸の数値は行内で
    いちばん大きいものを採る。サイズ（27）や型番（90）より価格の方が
    大きい、という前提を置いている。
    """
    # 値下げ表記「12000円→9800円」は、矢印の後ろが今の価格。
    # 「9800円（定価15000円）」のような書き方と両立させるため、
    # 矢印があるときだけ後ろ側を優先する。
    parts = _ARROW.split(text)
    if len(parts) > 1:
        price, rest = _try_unit_price(parts[-1])
        if price > 0:
            head = " ".join(parts[:-1])
            head = _MAN_PRICE.sub(" ", head)
            head = _PRICE_WITH_UNIT.sub(" ", head)
            return price, head + " " + rest

    price, rest = _try_unit_price(text)
    if price > 0:
        return price, rest

    best: re.Match[str] | None = None
    for candidate in _BARE_NUMBER.finditer(text):
        if best is None or _to_int(candidate.group(1)) > _to_int(best.group(1)):
            best = candidate
    if best is None:
        return 0, text
    return _to_int(best.group(1)), text[: best.start()] + " " + text[best.end() :]


def _clean_title(text: str) -> str:
    """価格や記号を抜いた残りを、タイトルとして整える。"""
    text = _SOLD_MARKERS.sub(" ", text)
    text = _ACTIVE_MARKERS.sub(" ", text)
    text = re.sub(r"[,\t|]+", " ", text)
    text = re.sub(r"^[\s\-–—:：]+|[\s\-–—:：]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_line(
    line: str,
    *,
    default_condition: str = "",
    default_sold: bool = True,
    source: str = "manual",
) -> ParsedLine:
    """雑な1行を SoldComp に読み替える。"""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return ParsedLine(raw=raw, error="空行またはコメント行")

    # 全角の数字・カンマ・記号を半角へ寄せてから読む。
    # 「９，８００円」を全角カンマで分断して 800 円と読み違えないため。
    text = unicodedata.normalize("NFKC", raw)

    sold = default_sold
    if _SOLD_MARKERS.search(text):
        sold = True
    elif _ACTIVE_MARKERS.search(text):
        sold = False

    sold_at, rest = _extract_date(text)
    condition, rest = _extract_condition(rest)
    price, rest = _extract_price(rest)

    if price <= 0:
        return ParsedLine(raw=raw, error="価格らしき数値が見つかりません")
    if price < MIN_COMP_PRICE:
        return ParsedLine(
            raw=raw,
            error=(
                f"価格が {price} 円と読めました。メルカリの下限 {MIN_COMP_PRICE} 円"
                "未満なので読み違いとみなします（例: 9800円 のように書いてください）"
            ),
        )

    return ParsedLine(
        raw=raw,
        comp=SoldComp(
            title=_clean_title(rest),
            price=price,
            sold=sold,
            condition=condition or default_condition,
            sold_at=sold_at if sold else None,
            source=source,
        ),
    )


def parse_lines(
    lines: Iterable[str],
    *,
    default_condition: str = "",
    default_sold: bool = True,
    source: str = "manual",
) -> tuple[list[SoldComp], list[ParsedLine]]:
    """複数行を読む。

    Returns:
        (読めたサンプル, 読めなかった行)。空行は失敗として数えない。
    """
    comps: list[SoldComp] = []
    failed: list[ParsedLine] = []
    for line in lines:
        parsed = parse_line(
            line,
            default_condition=default_condition,
            default_sold=default_sold,
            source=source,
        )
        if parsed.ok:
            comps.append(parsed.comp)  # type: ignore[arg-type]
        elif parsed.raw and not parsed.raw.startswith("#"):
            failed.append(parsed)
    return comps, failed


#: 桁区切り付き（9,800）または裸の数値。カンマは「3桁区切りとして
#: 成立する並びのときだけ」数値の一部とみなし、それ以外は区切り文字。
#: 「9,800, 11,500」を 9 / 800 / 11 / 500 に分裂させないため。
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+")


def parse_prices(
    text: str,
    *,
    default_condition: str = "",
    default_sold: bool = True,
    title: str = "",
) -> list[SoldComp]:
    """「9800,11500,8900」形式の価格列を読む。

    区切りはカンマ・読点・空白のどれでもよく、「9,800円 11,500円」のような
    桁区切り・単位付きも通る。メルカリの下限 300 円未満に読めた数値は
    区切りの読み違いとみなして捨てる。
    """
    text = unicodedata.normalize("NFKC", text)
    comps: list[SoldComp] = []
    for chunk in _AMOUNT.findall(text):
        price = _to_int(chunk)
        if price < MIN_COMP_PRICE:
            continue
        comps.append(
            SoldComp(
                title=title,
                price=price,
                sold=default_sold,
                condition=default_condition,
                source="manual",
            )
        )
    return comps


# ── 保存 ─────────────────────────────────────────────────────
def _canonical_condition(raw: str) -> str:
    """状態の表記ゆれを重複判定用に揃える。

    CSV には「目立った傷や汚れなし」、行入力からは「no_scratch」のように
    別の表記で入ってくるため、そのまま比べると同じ実績が二重登録される。
    """
    text = (raw or "").strip()
    if not text or text in CONDITIONS:
        return text
    for key, label in CONDITIONS.items():
        if label == text or label.replace("、", "") == text.replace("、", ""):
            return key
    return _CONDITION_WORDS.get(text, text)


def _comp_key(comp: SoldComp) -> tuple:
    """同じサンプルを二重に登録しないための鍵。

    売却日は鍵に含める。同じ商品が同じ価格で別の日に売れるのは普通で、
    それは別々の実績だから。同じ行を貼り直したときは日付も同じになるので
    重複除去は変わらず効く。
    """
    return (
        comp.price,
        comp.title.strip(),
        comp.sold,
        _canonical_condition(comp.condition),
        comp.sold_at,
    )


def load_existing(directory: str | Path, query: str) -> list[SoldComp]:
    """保存済みのサンプルを読む。無ければ空。

    JSON（``research add`` の追記先）に加えて、同じ検索語の CSV が
    手置きされていればその行も取り込む。追記後に書き出される JSON が
    CSV の内容を含む上位互換になるので、リサーチ側がどちらを読んでも
    「書き写したのに反映されない」事故が起きない。
    """
    directory = Path(directory)
    slug = slugify(query)
    seen: set[tuple] = set()
    comps: list[SoldComp] = []

    def absorb(items: Iterable[SoldComp]) -> None:
        for comp in items:
            key = _comp_key(comp)
            if key in seen:
                continue
            seen.add(key)
            comps.append(comp)

    target = directory / f"{slug}.json"
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = []
        if isinstance(data, dict):
            data = data.get("items") or data.get("comps") or []
        absorb(
            SoldComp.from_dict(row)
            for row in data
            if isinstance(row, dict) and int(row.get("price") or 0) > 0
        )

    csv_target = directory / f"{slug}.csv"
    if csv_target.exists():
        try:
            absorb(CsvFileProvider(csv_target).fetch(query, limit=10_000))
        except (OSError, ValueError):
            pass

    return comps


@dataclass
class AppendResult:
    """追記の結果。"""

    path: Path
    added: int
    skipped: int
    total: int


def append_comps(
    directory: str | Path,
    query: str,
    comps: Sequence[SoldComp],
    *,
    replace: bool = False,
) -> AppendResult:
    """サンプルを既存ファイルに足す。同じ内容の行は足さない。

    Args:
        replace: True なら既存を捨てて置き換える。
    """
    from .providers import save_comps

    existing = [] if replace else load_existing(directory, query)
    seen = {_comp_key(c) for c in existing}

    merged = list(existing)
    added = 0
    skipped = 0
    for comp in comps:
        key = _comp_key(comp)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        merged.append(comp)
        added += 1

    path = save_comps(directory, query, merged)
    return AppendResult(path=path, added=added, skipped=skipped, total=len(merged))
