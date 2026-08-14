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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from ..models import CONDITIONS, SoldComp
from .providers import slugify

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

#: 「9,800円」のように単位が付いた金額
_PRICE_WITH_UNIT = re.compile(r"(?<![\w.])(\d[\d,]*)\s*円")
#: 単独で置かれた数値（型番 "990v6" の 990 は拾わない）
_BARE_NUMBER = re.compile(r"(?<![\w.,])(\d[\d,]*)(?![\w,]*[\w])")

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


def _extract_price(text: str) -> tuple[int, str]:
    """価格を取り出し、残りの文字列と一緒に返す。

    「円」が付いていればそれを優先する。付いていない場合は
    行内でいちばん大きい数値を価格とみなす。サイズ（27）や
    型番（90）より価格の方が大きい、という前提を置いている。
    """
    match = _PRICE_WITH_UNIT.search(text)
    if match:
        return _to_int(match.group(1)), text[: match.start()] + " " + text[match.end() :]

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

    sold = default_sold
    if _SOLD_MARKERS.search(raw):
        sold = True
    elif _ACTIVE_MARKERS.search(raw):
        sold = False

    sold_at, rest = _extract_date(raw)
    condition, rest = _extract_condition(rest)
    price, rest = _extract_price(rest)

    if price <= 0:
        return ParsedLine(raw=raw, error="価格らしき数値が見つかりません")

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


def parse_prices(
    text: str,
    *,
    default_condition: str = "",
    default_sold: bool = True,
    title: str = "",
) -> list[SoldComp]:
    """「9800,11500,8900」形式の価格列を読む。"""
    comps: list[SoldComp] = []
    for chunk in re.split(r"[,\s、]+", text.strip()):
        if not chunk:
            continue
        digits = re.sub(r"[^\d]", "", chunk)
        if not digits:
            continue
        comps.append(
            SoldComp(
                title=title,
                price=int(digits),
                sold=default_sold,
                condition=default_condition,
                source="manual",
            )
        )
    return comps


# ── 保存 ─────────────────────────────────────────────────────
def _comp_key(comp: SoldComp) -> tuple:
    """同じサンプルを二重に登録しないための鍵。"""
    return (comp.price, comp.title.strip(), comp.sold, comp.condition)


def load_existing(directory: str | Path, query: str) -> list[SoldComp]:
    """保存済みの JSON サンプルを読む。無ければ空。

    CSV で置かれている場合はそちらを正としたいので、ここでは触らない。
    """
    target = Path(directory) / f"{slugify(query)}.json"
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("comps") or []
    comps: list[SoldComp] = []
    for row in data:
        if isinstance(row, dict) and int(row.get("price") or 0) > 0:
            comps.append(SoldComp.from_dict(row))
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
