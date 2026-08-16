"""出品写真の加工パイプライン。

スマホで撮った写真を、そのまま出品できる状態に整える:
  向きの補正 → 余白の切り詰め → 明るさ・色の自動補正 → 背景の白飛ばし
  → 正方形化 → リサイズ → 保存

各工程は独立して on/off できる。メソッドチェーンで書ける。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, ImageStat

# iPhone の HEIC を読めるようにする。未導入でも他形式は動く。
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:  # pragma: no cover - 環境依存
    _HEIF_AVAILABLE = False

#: メルカリ推奨の出品画像サイズ（正方形）
DEFAULT_SIZE = 1080
#: 背景処理用の作業解像度。ここまで縮めてからマスクを作る。
_MASK_WORK_SIZE = 480

RGB = tuple[int, int, int]


@dataclass
class ImageStats:
    """加工前後の状態を記録して、何が起きたか説明できるようにする。"""

    source: str = ""
    original_size: tuple[int, int] = (0, 0)
    final_size: tuple[int, int] = (0, 0)
    steps: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.steps is None:
            self.steps = []


class ImagePipeline:
    """1枚の画像に対する加工の連なり。

    使い方::

        ImagePipeline.open("photo.jpg").auto().save("out.jpg")
    """

    def __init__(self, image: Image.Image, source: str = "") -> None:
        self.image = image
        self.stats = ImageStats(
            source=source, original_size=image.size, final_size=image.size
        )

    # ── 入出力 ─────────────────────────────────────────────
    @classmethod
    def open(cls, path: str | Path) -> "ImagePipeline":
        path = Path(path)
        if path.suffix.lower() in (".heic", ".heif") and not _HEIF_AVAILABLE:
            raise ValueError(
                f"{path.name} は HEIC 形式です。読み込むには `pip install pillow-heif` を"
                "実行してください（iPhone側で「互換性優先」にして JPEG で撮る方法もあります）。"
            )
        image = Image.open(path)
        # EXIF の回転情報を実ピクセルに反映してから扱う
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        pipeline = cls(image, source=str(path))
        pipeline._log("EXIFの向きを補正")
        return pipeline

    def save(self, path: str | Path, quality: int = 92) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        image = self.image
        if path.suffix.lower() in (".jpg", ".jpeg") and image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])
            image = background
        image.save(path, quality=quality, optimize=True)
        self.stats.final_size = self.image.size
        return path

    def copy(self) -> "ImagePipeline":
        clone = ImagePipeline(self.image.copy(), self.stats.source)
        clone.stats.steps = list(self.stats.steps)
        return clone

    def _log(self, message: str) -> "ImagePipeline":
        self.stats.steps.append(message)
        return self

    # ── 幾何 ──────────────────────────────────────────────
    def trim_border(self, tolerance: int = 12, keep_margin: int = 0) -> "ImagePipeline":
        """周囲の均一な余白を切り落とし、被写体を大きく見せる。"""
        rgb = self.image.convert("RGB")
        # 左上の色を背景色とみなし、そこからの差が小さい領域を余白と判断する
        corner = rgb.getpixel((0, 0))
        background = Image.new("RGB", rgb.size, corner)
        diff = ImageChops.difference(rgb, background).convert("L")
        mask = diff.point(lambda p: 255 if p > tolerance else 0)
        bbox = mask.getbbox()
        if not bbox:
            return self._log("余白の切り詰め: 対象なし")
        if keep_margin:
            left, top, right, bottom = bbox
            bbox = (
                max(left - keep_margin, 0),
                max(top - keep_margin, 0),
                min(right + keep_margin, self.image.width),
                min(bottom + keep_margin, self.image.height),
            )
        before = self.image.size
        self.image = self.image.crop(bbox)
        return self._log(f"余白を切り詰め {before} → {self.image.size}")

    def square(self, background: RGB = (255, 255, 255)) -> "ImagePipeline":
        """余白を足して正方形にする。被写体を切らない。"""
        width, height = self.image.size
        if width == height:
            return self._log("正方形化: すでに正方形")
        side = max(width, height)
        mode = "RGBA" if self.image.mode == "RGBA" else "RGB"
        fill = background + (0,) if mode == "RGBA" else background
        canvas = Image.new(mode, (side, side), fill)  # type: ignore[arg-type]
        canvas.paste(
            self.image,
            ((side - width) // 2, (side - height) // 2),
            self.image if self.image.mode == "RGBA" else None,
        )
        self.image = canvas
        return self._log(f"正方形化 {width}x{height} → {side}x{side}")

    def resize(self, size: int = DEFAULT_SIZE) -> "ImagePipeline":
        """長辺を size に揃える。拡大はしない（画質が落ちるため）。"""
        width, height = self.image.size
        longest = max(width, height)
        if longest <= size:
            return self._log(f"リサイズ: 拡大しない（{width}x{height}）")
        ratio = size / longest
        new_size = (max(int(width * ratio), 1), max(int(height * ratio), 1))
        self.image = self.image.resize(new_size, Image.Resampling.LANCZOS)
        return self._log(f"リサイズ {width}x{height} → {new_size[0]}x{new_size[1]}")

    def rotate(self, degrees: float) -> "ImagePipeline":
        if degrees % 360 == 0:
            return self
        self.image = self.image.rotate(-degrees, expand=True, fillcolor=(255, 255, 255))
        return self._log(f"{degrees}度 回転")

    # ── 色・明るさ ──────────────────────────────────────────
    @staticmethod
    def _autocontrast(image: Image.Image, cutoff: float) -> Image.Image:
        """色相を保ったままヒストグラムを伸ばす。

        autocontrast は既定でチャンネルごとに正規化するため、そのまま使うと
        灰色の商品が青や緑に転ぶ。商品の色を誤って伝えるのは出品として致命的
        なので、輝度基準で伸ばす preserve_tone を使う。
        """
        try:
            return ImageOps.autocontrast(image, cutoff=cutoff, preserve_tone=True)
        except TypeError:  # Pillow 9.5 未満には preserve_tone が無い
            return ImageOps.autocontrast(image, cutoff=cutoff)

    def auto_levels(self, cutoff: float = 0.5) -> "ImagePipeline":
        """ヒストグラムを引き伸ばして、眠い写真をはっきりさせる。"""
        if self.image.mode == "RGBA":
            rgb, alpha = self.image.convert("RGB"), self.image.split()[3]
            rgb = self._autocontrast(rgb, cutoff)
            self.image = Image.merge("RGBA", (*rgb.split(), alpha))
        else:
            self.image = self._autocontrast(self.image, cutoff)
        return self._log(f"自動レベル補正 (cutoff={cutoff})")

    def white_balance(self, max_shift: float = 1.25) -> "ImagePipeline":
        """グレーワールド仮説で色かぶりを補正する。

        照明の色（電球の黄色かぶりなど）を打ち消し、商品の色を実物に近づける。

        グレーワールドは「画面全体を平均すると無彩色になる」という前提なので、
        画面の大半が1色の商品（真っ赤なワンピースなど）では前提が崩れ、
        その色を打ち消す方向に暴走する。max_shift で補正量に上限をかけ、
        照明のかぶりは取りつつ商品本来の色は残す。
        """
        rgb = self.image.convert("RGB")
        # 縮小してから平均を取る。フル解像度で走査する必要はない。
        sample = rgb.resize((64, 64), Image.Resampling.BILINEAR)
        r_avg, g_avg, b_avg = ImageStat.Stat(sample).mean
        gray = (r_avg + g_avg + b_avg) / 3
        if min(r_avg, g_avg, b_avg) < 1:
            return self._log("ホワイトバランス: 補正不要")

        floor = 1.0 / max_shift

        def make_lut(average: float) -> list[int]:
            scale = max(floor, min(max_shift, gray / average))
            return [min(255, int(i * scale)) for i in range(256)]

        lut = make_lut(r_avg) + make_lut(g_avg) + make_lut(b_avg)
        corrected = rgb.point(lut)
        if self.image.mode == "RGBA":
            self.image = Image.merge("RGBA", (*corrected.split(), self.image.split()[3]))
        else:
            self.image = corrected
        return self._log(f"ホワイトバランス補正 (上限×{max_shift})")

    def enhance(
        self,
        brightness: float = 1.0,
        contrast: float = 1.0,
        color: float = 1.0,
        sharpness: float = 1.0,
    ) -> "ImagePipeline":
        """明るさ・コントラスト・彩度・シャープネスを手動で調整する。"""
        applied = []
        for name, value, enhancer in (
            ("明るさ", brightness, ImageEnhance.Brightness),
            ("コントラスト", contrast, ImageEnhance.Contrast),
            ("彩度", color, ImageEnhance.Color),
            ("シャープ", sharpness, ImageEnhance.Sharpness),
        ):
            if abs(value - 1.0) > 1e-6:
                self.image = enhancer(self.image).enhance(value)
                applied.append(f"{name}×{value}")
        if applied:
            self._log("手動調整: " + ", ".join(applied))
        return self

    def auto_brightness(self, target: float = 168.0, max_shift: float = 1.6) -> "ImagePipeline":
        """暗すぎ/明るすぎの写真を、目標の平均輝度に寄せる。"""
        gray = self.image.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
        current = ImageStat.Stat(gray).mean[0]
        if current < 1:
            return self._log("明るさ自動補正: 真っ黒のためスキップ")
        factor = max(1 / max_shift, min(max_shift, target / current))
        if abs(factor - 1.0) < 0.04:
            return self._log(f"明るさ自動補正: 不要 (平均輝度 {current:.0f})")
        self.image = ImageEnhance.Brightness(self.image).enhance(factor)
        return self._log(f"明るさ自動補正 ×{factor:.2f} (平均輝度 {current:.0f} → 目標 {target:.0f})")

    def sharpen(self, strength: float = 1.3) -> "ImagePipeline":
        self.image = self.image.filter(
            ImageFilter.UnsharpMask(radius=2, percent=int(60 * strength), threshold=3)
        )
        return self._log(f"アンシャープマスク ×{strength}")

    # ── 背景処理 ────────────────────────────────────────────
    def _background_mask(self, tolerance: int) -> Image.Image:
        """四隅から色の近い領域を塗り広げて、背景マスクを作る。

        処理は縮小画像上で行い、結果のマスクだけ元サイズに戻す。
        フルサイズで塗り広げると数秒〜数十秒かかるため。
        """
        work = self.image.convert("RGB")
        scale = max(work.width, work.height) / _MASK_WORK_SIZE
        if scale > 1:
            work = work.resize(
                (max(int(work.width / scale), 1), max(int(work.height / scale), 1)),
                Image.Resampling.BILINEAR,
            )
        width, height = work.size
        pixels = work.load()

        seeds = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
        seed_colors = [pixels[x, y] for x, y in seeds]

        visited = bytearray(width * height)
        queue: deque[tuple[int, int]] = deque()
        for (x, y), color in zip(seeds, seed_colors):
            index = y * width + x
            if not visited[index]:
                visited[index] = 1
                queue.append((x, y))

        def close_enough(color: Sequence[int]) -> bool:
            for seed in seed_colors:
                distance = math.sqrt(
                    (color[0] - seed[0]) ** 2
                    + (color[1] - seed[1]) ** 2
                    + (color[2] - seed[2]) ** 2
                )
                if distance <= tolerance:
                    return True
            return False

        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < width and 0 <= ny < height):
                    continue
                index = ny * width + nx
                if visited[index]:
                    continue
                if close_enough(pixels[nx, ny]):
                    visited[index] = 1
                    queue.append((nx, ny))

        # 背景=0 / 被写体=255 のマスク
        mask = Image.frombytes(
            "L", (width, height), bytes(0 if v else 255 for v in visited)
        )
        mask = mask.filter(ImageFilter.GaussianBlur(radius=1.2))
        if mask.size != self.image.size:
            mask = mask.resize(self.image.size, Image.Resampling.BILINEAR)
        return mask

    def whiten_background(self, tolerance: int = 42) -> "ImagePipeline":
        """背景を真っ白に飛ばす。物撮りの見栄えが一段良くなる。

        壁や机など、四隅から連続した均一な背景に有効。柄物の背景や
        被写体が画面端に接している写真では期待通りにならないことがある。
        """
        mask = self._background_mask(tolerance)
        white = Image.new("RGB", self.image.size, (255, 255, 255))
        base = self.image.convert("RGB")
        self.image = Image.composite(base, white, mask)
        return self._log(f"背景を白飛ばし (許容差={tolerance})")

    def remove_background(self, tolerance: int = 42) -> "ImagePipeline":
        """背景を透過させる（PNG 保存用）。"""
        mask = self._background_mask(tolerance)
        base = self.image.convert("RGBA")
        base.putalpha(mask)
        self.image = base
        return self._log(f"背景を透過 (許容差={tolerance})")

    # ── まとめ ─────────────────────────────────────────────
    def auto(
        self,
        size: int = DEFAULT_SIZE,
        whiten: bool = False,
        tolerance: int = 42,
    ) -> "ImagePipeline":
        """出品写真としてまず外さない標準の加工。"""
        self.trim_border(keep_margin=8)
        self.white_balance()
        self.auto_brightness()
        self.auto_levels(cutoff=0.4)
        if whiten:
            self.whiten_background(tolerance=tolerance)
        self.sharpen(strength=1.0)
        self.square()
        self.resize(size)
        return self


def process_batch(
    paths: Iterable[str | Path],
    output_dir: str | Path,
    *,
    size: int = DEFAULT_SIZE,
    whiten: bool = False,
    tolerance: int = 42,
    prefix: str = "",
) -> list[tuple[Path, ImageStats]]:
    """複数の写真をまとめて加工する。

    Returns:
        (出力パス, 加工ログ) のリスト
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, ImageStats]] = []
    for index, path in enumerate(paths, start=1):
        pipeline = ImagePipeline.open(path)
        pipeline.auto(size=size, whiten=whiten, tolerance=tolerance)
        name = f"{prefix}{index:02d}.jpg" if prefix else f"{Path(path).stem}_edited.jpg"
        saved = pipeline.save(output_dir / name)
        results.append((saved, pipeline.stats))
    return results
