"""サムネイル（1枚目の画像）の生成。

メルカリは検索結果が正方形のサムネイル一覧で表示されるため、
1枚目でクリックされるかどうかが閲覧数をほぼ決める。
商品写真に短い訴求テキストを重ねた1080x1080を作る。

やらないこと: 実物と異なる印象を与える加工（過度な色補正、比率の変更、
存在しない付属品の描画）。クリック率は上がっても評価が下がるため。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from PIL import Image, ImageDraw, ImageFilter

from .fonts import load_font

DEFAULT_CANVAS = 1080

Position = Literal["top_band", "bottom_band", "top_left", "top_right", "bottom_left", "bottom_right"]

RGB = tuple[int, int, int]

#: 使い回せる配色
PALETTES: dict[str, dict[str, RGB]] = {
    "red": {"bg": (229, 57, 53), "fg": (255, 255, 255)},
    "navy": {"bg": (26, 45, 82), "fg": (255, 255, 255)},
    "green": {"bg": (30, 136, 92), "fg": (255, 255, 255)},
    "orange": {"bg": (245, 124, 0), "fg": (255, 255, 255)},
    "black": {"bg": (28, 28, 30), "fg": (255, 255, 255)},
    "yellow": {"bg": (250, 204, 21), "fg": (28, 28, 30)},
}


@dataclass
class Badge:
    """サムネイルに重ねる文字要素。"""

    text: str
    position: Position = "top_band"
    palette: str = "red"
    #: キャンバス幅に対する文字サイズの比率
    font_scale: float = 0.062
    #: 独自色を使う場合（palette より優先）
    bg_color: RGB | None = None
    fg_color: RGB | None = None

    def colors(self) -> tuple[RGB, RGB]:
        preset = PALETTES.get(self.palette, PALETTES["red"])
        return (self.bg_color or preset["bg"], self.fg_color or preset["fg"])


class ThumbnailBuilder:
    """商品写真からサムネイルを組み立てる。"""

    def __init__(
        self,
        canvas_size: int = DEFAULT_CANVAS,
        background: RGB = (255, 255, 255),
    ) -> None:
        self.size = canvas_size
        self.background = background
        self.canvas = Image.new("RGB", (canvas_size, canvas_size), background)

    # ── 背景 ──────────────────────────────────────────────
    def with_blurred_backdrop(self, photo: Image.Image, blur: int = 24) -> "ThumbnailBuilder":
        """商品写真をぼかして敷き、余白の白さを消す。

        縦長・横長の写真でも上下の白帯が目立たなくなる。
        """
        backdrop = photo.convert("RGB").copy()
        # キャンバスを覆うように拡大してから中央を切り出す
        ratio = max(self.size / backdrop.width, self.size / backdrop.height)
        backdrop = backdrop.resize(
            (max(int(backdrop.width * ratio), 1), max(int(backdrop.height * ratio), 1)),
            Image.Resampling.LANCZOS,
        )
        left = (backdrop.width - self.size) // 2
        top = (backdrop.height - self.size) // 2
        backdrop = backdrop.crop((left, top, left + self.size, top + self.size))
        backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=blur))
        # 白を少し重ねて主役を邪魔しないようにする
        veil = Image.new("RGB", backdrop.size, (255, 255, 255))
        self.canvas = Image.blend(backdrop, veil, alpha=0.45)
        return self

    # ── 商品写真 ────────────────────────────────────────────
    def place_photo(
        self,
        photo: Image.Image,
        margin_ratio: float = 0.08,
        shadow: bool = True,
    ) -> "ThumbnailBuilder":
        """商品写真を中央に配置する。切り抜かず全体を収める。"""
        inner = int(self.size * (1 - margin_ratio * 2))
        image = photo.copy()
        image.thumbnail((inner, inner), Image.Resampling.LANCZOS)

        x = (self.size - image.width) // 2
        y = (self.size - image.height) // 2

        if shadow and image.mode != "RGBA":
            shadow_layer = Image.new("RGBA", self.canvas.size, (0, 0, 0, 0))
            drawer = ImageDraw.Draw(shadow_layer)
            pad = int(self.size * 0.012)
            drawer.rectangle(
                [x - pad, y - pad, x + image.width + pad, y + image.height + pad],
                fill=(0, 0, 0, 40),
            )
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=pad))
            self.canvas = Image.alpha_composite(
                self.canvas.convert("RGBA"), shadow_layer
            ).convert("RGB")

        self.canvas.paste(image, (x, y), image if image.mode == "RGBA" else None)
        return self

    # ── テキスト ────────────────────────────────────────────
    def add_badge(self, badge: Badge) -> "ThumbnailBuilder":
        """訴求テキストを重ねる。"""
        if not badge.text.strip():
            return self
        bg_color, fg_color = badge.colors()
        font_size = max(int(self.size * badge.font_scale), 12)
        font = load_font(font_size, bold_hint=True)
        draw = ImageDraw.Draw(self.canvas)

        if badge.position in ("top_band", "bottom_band"):
            self._draw_band(draw, badge.text, font, bg_color, fg_color, badge.position)
        else:
            self._draw_corner(draw, badge.text, font, bg_color, fg_color, badge.position)
        return self

    def add_badges(self, badges: Sequence[Badge]) -> "ThumbnailBuilder":
        for badge in badges:
            self.add_badge(badge)
        return self

    def _text_size(self, draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top

    def _draw_band(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font,
        bg_color: RGB,
        fg_color: RGB,
        position: Position,
    ) -> None:
        text_w, text_h = self._text_size(draw, text, font)
        padding = int(self.size * 0.026)
        band_h = text_h + padding * 2

        if position == "top_band":
            top = 0
        else:
            top = self.size - band_h

        draw.rectangle([0, top, self.size, top + band_h], fill=bg_color)
        # 描画位置は textbbox の原点ずれを吸収するため anchor を使う
        draw.text(
            (self.size // 2, top + band_h // 2),
            text,
            font=font,
            fill=fg_color,
            anchor="mm",
        )

    def _draw_corner(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font,
        bg_color: RGB,
        fg_color: RGB,
        position: Position,
    ) -> None:
        text_w, text_h = self._text_size(draw, text, font)
        pad_x = int(self.size * 0.022)
        pad_y = int(self.size * 0.016)
        box_w = text_w + pad_x * 2
        box_h = text_h + pad_y * 2
        margin = int(self.size * 0.035)

        if position == "top_left":
            x0, y0 = margin, margin
        elif position == "top_right":
            x0, y0 = self.size - margin - box_w, margin
        elif position == "bottom_left":
            x0, y0 = margin, self.size - margin - box_h
        else:  # bottom_right
            x0, y0 = self.size - margin - box_w, self.size - margin - box_h

        radius = int(box_h * 0.22)
        draw.rounded_rectangle(
            [x0, y0, x0 + box_w, y0 + box_h], radius=radius, fill=bg_color
        )
        draw.text(
            (x0 + box_w // 2, y0 + box_h // 2), text, font=font, fill=fg_color, anchor="mm"
        )

    # ── 仕上げ ─────────────────────────────────────────────
    def add_border(self, width: int = 4, color: RGB = (225, 225, 230)) -> "ThumbnailBuilder":
        draw = ImageDraw.Draw(self.canvas)
        draw.rectangle(
            [0, 0, self.size - 1, self.size - 1], outline=color, width=width
        )
        return self

    def save(self, path: str | Path, quality: int = 94) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.canvas.save(path, quality=quality, optimize=True)
        return path

    # ── かんたん生成 ────────────────────────────────────────
    @classmethod
    def build(
        cls,
        photo_path: str | Path,
        output_path: str | Path,
        *,
        top_text: str = "",
        corner_text: str = "",
        bottom_text: str = "",
        palette: str = "red",
        canvas_size: int = DEFAULT_CANVAS,
        blurred_backdrop: bool = True,
    ) -> Path:
        """写真1枚から、よくある構成のサムネイルを1発で作る。"""
        photo = Image.open(photo_path)
        from PIL import ImageOps

        photo = ImageOps.exif_transpose(photo)

        builder = cls(canvas_size=canvas_size)
        if blurred_backdrop:
            builder.with_blurred_backdrop(photo)
        # 帯を置くぶん、写真は少し内側に寄せる
        margin = 0.12 if (top_text or bottom_text) else 0.07
        builder.place_photo(photo, margin_ratio=margin)

        badges = []
        if top_text:
            badges.append(Badge(top_text, "top_band", palette))
        if corner_text:
            badges.append(
                Badge(corner_text, "top_right", "black", font_scale=0.048)
            )
        if bottom_text:
            badges.append(
                Badge(bottom_text, "bottom_band", "navy", font_scale=0.05)
            )
        builder.add_badges(badges)
        builder.add_border()
        return builder.save(output_path)


def suggest_badges(product, price: int | None = None) -> dict[str, str]:
    """商品情報から、サムネイルに載せる文言の候補を作る。

    事実として言えることだけを返す。「激安」「大人気」のような
    根拠のない煽り文句は入れない。
    """
    from ..models import CONDITIONS

    top = ""
    if getattr(product, "shipping_payer", "seller") == "seller":
        top = "送料無料"
    if price:
        top = f"{top}　{price:,}円" if top else f"{price:,}円"

    corner = ""
    condition = getattr(product, "condition", "")
    if condition in ("new", "like_new"):
        corner = CONDITIONS[condition]
    elif condition == "no_scratch":
        corner = "美品"

    bottom = ""
    points = getattr(product, "selling_points", None) or []
    if points:
        bottom = points[0][:18]
    elif getattr(product, "brand", ""):
        bottom = product.brand

    return {"top_text": top, "corner_text": corner, "bottom_text": bottom}
