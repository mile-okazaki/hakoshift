"""JSON ファイルによる永続化。

外部DBを立てずに済ませるための最小限のストア。書き込みは一時ファイル経由の
アトミック置換にして、途中で落ちても既存データが壊れないようにしている。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, Iterator, TypeVar

T = TypeVar("T")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def write_json(path: Path, data: Any) -> None:
    """アトミックに JSON を書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class JsonCollection(Generic[T]):
    """dataclass のリストを1ファイルに保存するコレクション。

    ``key`` は各レコードを一意に識別する属性名（例: "sku", "id"）。
    """

    def __init__(
        self,
        path: Path,
        factory: Callable[[dict[str, Any]], T],
        key: str = "id",
    ) -> None:
        self.path = Path(path)
        self._factory = factory
        self._key = key
        self._items: list[T] | None = None

    # ── 読み書き ────────────────────────────────────────────
    def _load(self) -> list[T]:
        if self._items is None:
            raw = read_json(self.path, default=[]) or []
            self._items = [self._factory(row) for row in raw]
        return self._items

    def reload(self) -> None:
        """ディスクの内容を読み直す。"""
        self._items = None

    def save(self) -> None:
        items = self._load()
        write_json(self.path, [self._as_dict(i) for i in items])

    @staticmethod
    def _as_dict(item: T) -> dict[str, Any]:
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        raise TypeError(f"{type(item).__name__} に to_dict() がありません")

    def _key_of(self, item: T) -> Any:
        return getattr(item, self._key)

    # ── コレクション操作 ────────────────────────────────────
    def all(self) -> list[T]:
        return list(self._load())

    def __iter__(self) -> Iterator[T]:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def get(self, key_value: Any) -> T | None:
        for item in self._load():
            if self._key_of(item) == key_value:
                return item
        return None

    def find(self, predicate: Callable[[T], bool]) -> list[T]:
        return [i for i in self._load() if predicate(i)]

    def upsert(self, item: T) -> T:
        """同じキーがあれば置き換え、なければ追加する。"""
        items = self._load()
        key_value = self._key_of(item)
        for idx, existing in enumerate(items):
            if self._key_of(existing) == key_value:
                items[idx] = item
                break
        else:
            items.append(item)
        self.save()
        return item

    def add_many(self, new_items: Iterable[T]) -> int:
        items = self._load()
        count = 0
        for item in new_items:
            items.append(item)
            count += 1
        self.save()
        return count

    def delete(self, key_value: Any) -> bool:
        items = self._load()
        for idx, existing in enumerate(items):
            if self._key_of(existing) == key_value:
                del items[idx]
                self.save()
                return True
        return False
