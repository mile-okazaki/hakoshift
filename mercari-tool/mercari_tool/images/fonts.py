"""日本語フォントの探索。

サムネイルに日本語を入れるので、環境にあるフォントを自動で見つける。
見つからない場合は PIL の既定フォントに落とすが、その場合日本語は
豆腐（□）になるため呼び出し側に分かるよう警告できるようにしている。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

#: 探索する候補。上にあるものほど見た目が良い。
FONT_CANDIDATES: list[str] = [
    # 環境変数で明示指定されていれば最優先
    # (get_font_path 内で処理)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]


@lru_cache(maxsize=1)
def find_japanese_font() -> str | None:
    """使える日本語フォントのパスを返す。無ければ None。"""
    override = os.getenv("MERCARI_FONT_PATH")
    if override and Path(override).exists():
        return override
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    # 最後の手段としてシステム全体を軽く走査する
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts")):
        base = Path(root)
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() in (".ttf", ".otf", ".ttc"):
                name = path.name.lower()
                if any(k in name for k in ("gothic", "noto", "cjk", "ipa", "meiryo", "yu")):
                    return str(path)
    return None


def load_font(size: int, bold_hint: bool = False) -> ImageFont.FreeTypeFont:
    """指定サイズのフォントを読み込む。

    日本語フォントが見つからない環境では PIL の既定フォントを返す。
    その場合、日本語は表示できない。
    """
    path = find_japanese_font()
    if path:
        try:
            # .ttc は複数フォントを含むため、Bold 寄りのインデックスを試す
            index = 1 if (bold_hint and path.lower().endswith(".ttc")) else 0
            return ImageFont.truetype(path, size=size, index=index)
        except (OSError, ValueError):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def has_japanese_font() -> bool:
    return find_japanese_font() is not None
