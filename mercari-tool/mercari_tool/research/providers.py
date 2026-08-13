"""相場サンプルの取得元。

メルカリには公開の検索APIが無く、スクレイピングは利用規約で禁止されている。
そのため既定は「利用者が手元に用意したデータ（CSV/JSON）を読む」方式にしている。

外部エンドポイントから取得したい場合は HttpJsonProvider を使えるが、
MERCARI_ALLOW_HTTP_RESEARCH=1 の明示的な opt-in が必要で、
取得先の規約遵守は利用者の責任になる。
"""

from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..models import SoldComp

_TRUE = {"1", "true", "yes", "y", "sold", "売却済み", "売り切れ", "sold_out", "○"}


def slugify(text: str) -> str:
    """クエリからファイル名に使える文字列を作る。"""
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    slug = re.sub(r"[^\w぀-ヿ一-鿿]+", "-", normalized)
    return slug.strip("-") or "query"


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = re.sub(r"[^\d\-]", "", str(value))
    return int(cleaned) if cleaned not in ("", "-") else 0


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


class MarketDataProvider(ABC):
    """相場サンプルの取得元インターフェース。"""

    name: str = "provider"

    @abstractmethod
    def fetch(self, query: str, limit: int = 100) -> list[SoldComp]:
        """クエリに対応する販売実績サンプルを返す。"""


# ── ファイルから読む ─────────────────────────────────────────
class CsvFileProvider(MarketDataProvider):
    """CSV から読み込む。

    想定する列（過不足は許容）::

        title,price,sold,condition,sold_at,listed_at,url

    ``price`` 以外はすべて省略可能。日本語ヘッダー（商品名/価格/状態/売却日）にも対応。
    """

    name = "csv"

    #: 日本語ヘッダーを内部名に寄せる
    HEADER_ALIASES = {
        "商品名": "title",
        "タイトル": "title",
        "名前": "title",
        "価格": "price",
        "販売価格": "price",
        "売値": "price",
        "金額": "price",
        "状態": "condition",
        "商品の状態": "condition",
        "売却": "sold",
        "売却済み": "sold",
        "販売状況": "sold",
        "売却日": "sold_at",
        "販売日": "sold_at",
        "出品日": "listed_at",
        "url": "url",
        "リンク": "url",
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self, query: str, limit: int = 100) -> list[SoldComp]:
        if not self.path.exists():
            raise FileNotFoundError(f"相場データが見つかりません: {self.path}")
        rows: list[SoldComp] = []
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                row = {
                    self.HEADER_ALIASES.get((k or "").strip(), (k or "").strip()): v
                    for k, v in raw.items()
                }
                price = _to_int(row.get("price"))
                if price <= 0:
                    continue
                # sold 列が存在するなら、空欄は「まだ売れていない」を意味する。
                # 列そのものが無い CSV は、全行が売却済みサンプルとみなす。
                sold = _to_bool(row.get("sold"), default=False) if "sold" in row else True
                rows.append(
                    SoldComp.from_dict(
                        {
                            "title": (row.get("title") or "").strip(),
                            "price": price,
                            "sold": sold,
                            "condition": (row.get("condition") or "").strip(),
                            "sold_at": row.get("sold_at"),
                            "listed_at": row.get("listed_at"),
                            "url": (row.get("url") or "").strip(),
                            "source": self.name,
                        }
                    )
                )
                if len(rows) >= limit:
                    break
        return rows


class JsonFileProvider(MarketDataProvider):
    """SoldComp の配列（またはそれを含む dict）を持つ JSON を読む。"""

    name = "json"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self, query: str, limit: int = 100) -> list[SoldComp]:
        if not self.path.exists():
            raise FileNotFoundError(f"相場データが見つかりません: {self.path}")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("items") or data.get("comps") or []
        comps = []
        for row in data[:limit]:
            if _to_int(row.get("price")) <= 0:
                continue
            row = dict(row)
            row.setdefault("source", self.name)
            row["price"] = _to_int(row["price"])
            comps.append(SoldComp.from_dict(row))
        return comps


class DirectoryProvider(MarketDataProvider):
    """クエリ名に対応するファイルをディレクトリから探す。

    ``data/comps/`` に ``ナイキ-スニーカー.csv`` のように置いておけば、
    ``research "ナイキ スニーカー"`` でそのまま読み込まれる。
    """

    name = "directory"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(self, query: str) -> Path | None:
        slug = slugify(query)
        for ext in (".csv", ".json"):
            candidate = self.directory / f"{slug}{ext}"
            if candidate.exists():
                return candidate
        return None

    def fetch(self, query: str, limit: int = 100) -> list[SoldComp]:
        target = self.path_for(query)
        if target is None:
            raise FileNotFoundError(
                f"'{query}' の相場データがありません。\n"
                f"  {self.directory / (slugify(query) + '.csv')} に\n"
                f"  title,price,sold,condition,sold_at,listed_at\n"
                f"  の形式で保存してから再実行してください。"
            )
        provider: MarketDataProvider = (
            CsvFileProvider(target) if target.suffix == ".csv" else JsonFileProvider(target)
        )
        return provider.fetch(query, limit)


class ManualPriceProvider(MarketDataProvider):
    """価格の数値だけをその場で渡す簡易プロバイダ。"""

    name = "manual"

    def __init__(self, prices: Sequence[int], sold: bool = True) -> None:
        self.prices = [int(p) for p in prices if int(p) > 0]
        self.sold = sold

    def fetch(self, query: str, limit: int = 100) -> list[SoldComp]:
        return [
            SoldComp(title=query, price=p, sold=self.sold, source=self.name)
            for p in self.prices[:limit]
        ]


# ── HTTP から読む（opt-in）────────────────────────────────────
class HttpJsonProvider(MarketDataProvider):
    """JSON を返す任意のエンドポイントから相場サンプルを取得する。

    エンドポイントもフィールド対応も設定で渡す。特定サイトの内部APIを
    ハードコードしていないのは、規約と仕様変更の両方を利用者側で
    コントロールできるようにするため。

    利用にあたっては取得先の利用規約・robots.txt を必ず確認すること。
    """

    name = "http"

    def __init__(
        self,
        endpoint: str,
        *,
        interval_sec: float = 3.0,
        timeout_sec: float = 15.0,
        items_path: str = "items",
        field_map: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        query_param: str = "keyword",
    ) -> None:
        if not endpoint:
            raise ValueError("エンドポイントが未設定です（MERCARI_RESEARCH_ENDPOINT）")
        self.endpoint = endpoint
        self.interval_sec = max(interval_sec, 1.0)
        self.timeout_sec = timeout_sec
        self.items_path = items_path
        self.query_param = query_param
        self.field_map = field_map or {
            "title": "name",
            "price": "price",
            "sold": "sold",
            "condition": "condition",
            "sold_at": "updated",
            "url": "url",
        }
        self.headers = headers or {
            "Accept": "application/json",
            "User-Agent": "mercari-tool/0.1 (research)",
        }
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _dig(data: Any, path: str) -> Any:
        """"a.b.c" 形式で入れ子の値を取り出す。"""
        current = data
        for part in path.split("."):
            if not part:
                continue
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current

    def fetch(self, query: str, limit: int = 100) -> list[SoldComp]:
        self._throttle()
        separator = "&" if "?" in self.endpoint else "?"
        url = (
            f"{self.endpoint}{separator}"
            f"{urllib.parse.urlencode({self.query_param: query, 'limit': limit})}"
        )
        request = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"相場取得に失敗しました (HTTP {exc.code}): {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"相場取得に失敗しました: {exc.reason}") from exc

        items = self._dig(payload, self.items_path) if self.items_path else payload
        if not isinstance(items, list):
            raise RuntimeError(
                f"レスポンスの '{self.items_path}' が配列ではありません。"
                "items_path / field_map の設定を確認してください。"
            )

        comps: list[SoldComp] = []
        for item in items[:limit]:
            price = _to_int(self._dig(item, self.field_map.get("price", "price")))
            if price <= 0:
                continue
            comps.append(
                SoldComp.from_dict(
                    {
                        "title": str(self._dig(item, self.field_map.get("title", "name")) or ""),
                        "price": price,
                        "sold": _to_bool(self._dig(item, self.field_map.get("sold", "sold")), True),
                        "condition": str(
                            self._dig(item, self.field_map.get("condition", "condition")) or ""
                        ),
                        "sold_at": self._dig(item, self.field_map.get("sold_at", "")),
                        "url": str(self._dig(item, self.field_map.get("url", "url")) or ""),
                        "source": self.name,
                    }
                )
            )
        return comps


# ── 組み立て・保存 ───────────────────────────────────────────
def build_provider(config) -> MarketDataProvider:
    """Config から既定のプロバイダを組み立てる。

    HTTP取得が明示的に許可され、かつエンドポイントが設定されているときだけ
    HttpJsonProvider を使う。それ以外はローカルファイルを見る。
    """
    if config.allow_http_research and config.research_endpoint:
        return HttpJsonProvider(
            config.research_endpoint, interval_sec=config.research_interval_sec
        )
    return DirectoryProvider(config.comps_dir)


def save_comps(directory: str | Path, query: str, comps: Iterable[SoldComp]) -> Path:
    """取得したサンプルを次回以降のために保存する。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{slugify(query)}.json"
    payload = {"query": query, "items": [c.to_dict() for c in comps]}
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
