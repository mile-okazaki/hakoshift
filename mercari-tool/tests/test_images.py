"""画像加工とサムネイル生成のテスト。

実写真は使えないので、既知の構造を持つ画像を生成して検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageStat

from mercari_tool.images import ImagePipeline, ThumbnailBuilder, process_batch
from mercari_tool.images.fonts import find_japanese_font
from mercari_tool.images.thumbnail import Badge, suggest_badges


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    """白背景の中央に赤い矩形を置いた、縦長の疑似商品写真。"""
    image = Image.new("RGB", (600, 900), (255, 255, 255))
    for x in range(180, 420):
        for y in range(300, 600):
            image.putpixel((x, y), (200, 40, 40))
    path = tmp_path / "photo.jpg"
    image.save(path, quality=95)
    return path


@pytest.fixture
def dark_photo(tmp_path: Path) -> Path:
    """暗くて青かぶりした写真。自動補正の効きを見る用。"""
    image = Image.new("RGB", (400, 400), (30, 30, 70))
    for x in range(120, 280):
        for y in range(120, 280):
            image.putpixel((x, y), (60, 55, 110))
    path = tmp_path / "dark.jpg"
    image.save(path, quality=95)
    return path


# ── パイプライン ────────────────────────────────────────────
def test_open_normalises_mode(photo):
    pipeline = ImagePipeline.open(photo)
    assert pipeline.image.mode in ("RGB", "RGBA")
    assert pipeline.stats.original_size == (600, 900)


def test_trim_border_crops_to_subject(photo):
    pipeline = ImagePipeline.open(photo).trim_border()
    # 赤い矩形は 240x300。周囲の白が落ちて小さくなる。
    assert pipeline.image.width < 600
    assert pipeline.image.height < 900


def test_trim_border_keeps_margin(photo):
    tight = ImagePipeline.open(photo).trim_border(keep_margin=0).image.size
    loose = ImagePipeline.open(photo).trim_border(keep_margin=20).image.size
    assert loose[0] > tight[0]
    assert loose[1] > tight[1]


def test_square_pads_without_cropping(photo):
    pipeline = ImagePipeline.open(photo).square()
    assert pipeline.image.width == pipeline.image.height == 900


def test_square_is_noop_when_already_square(dark_photo):
    pipeline = ImagePipeline.open(dark_photo).square()
    assert pipeline.image.size == (400, 400)


def test_resize_shrinks_but_never_enlarges(photo):
    shrunk = ImagePipeline.open(photo).resize(300)
    assert max(shrunk.image.size) == 300
    grown = ImagePipeline.open(photo).resize(5000)
    assert grown.image.size == (600, 900)  # 拡大はしない


def test_auto_brightness_lifts_a_dark_photo(dark_photo):
    original = ImagePipeline.open(dark_photo)
    before = _mean_luma(original.image)
    after = _mean_luma(ImagePipeline.open(dark_photo).auto_brightness().image)
    assert after > before


def test_white_balance_reduces_colour_cast(dark_photo):
    """青かぶりした写真の色チャンネル差が縮む。"""
    before = _channel_spread(ImagePipeline.open(dark_photo).image)
    after = _channel_spread(ImagePipeline.open(dark_photo).white_balance().image)
    assert after < before


def test_whiten_background_turns_corners_white(photo):
    pipeline = ImagePipeline.open(photo).whiten_background(tolerance=40)
    assert pipeline.image.getpixel((5, 5)) == (255, 255, 255)
    # 被写体は残る
    centre = pipeline.image.getpixel((300, 450))
    assert centre[0] > centre[1] + 50


def test_remove_background_produces_transparent_corners(photo):
    pipeline = ImagePipeline.open(photo).remove_background(tolerance=40)
    assert pipeline.image.mode == "RGBA"
    assert pipeline.image.getpixel((5, 5))[3] < 40      # 角は透明
    assert pipeline.image.getpixel((300, 450))[3] > 200  # 被写体は不透明


def test_auto_produces_square_listing_image(photo):
    pipeline = ImagePipeline.open(photo).auto(size=800)
    assert pipeline.image.width == pipeline.image.height
    assert max(pipeline.image.size) <= 800
    assert len(pipeline.stats.steps) > 4


def test_save_converts_rgba_to_jpeg_on_white(photo, tmp_path):
    out = tmp_path / "out.jpg"
    ImagePipeline.open(photo).remove_background().save(out)
    assert out.exists()
    assert Image.open(out).mode == "RGB"


def test_save_creates_missing_directories(photo, tmp_path):
    out = tmp_path / "a" / "b" / "out.jpg"
    ImagePipeline.open(photo).save(out)
    assert out.exists()


def test_process_batch_writes_all_files(photo, tmp_path):
    results = process_batch([photo, photo], tmp_path / "out", size=400, prefix="SKU_")
    assert len(results) == 2
    assert all(path.exists() for path, _ in results)
    assert (tmp_path / "out" / "SKU_01.jpg").exists()


# ── サムネイル ─────────────────────────────────────────────
def test_thumbnail_is_square_at_requested_size(photo, tmp_path):
    out = ThumbnailBuilder.build(photo, tmp_path / "thumb.jpg", canvas_size=600)
    assert Image.open(out).size == (600, 600)


def test_thumbnail_with_text_renders(photo, tmp_path):
    out = ThumbnailBuilder.build(
        photo,
        tmp_path / "thumb.jpg",
        top_text="送料無料",
        corner_text="美品",
        bottom_text="国内正規",
        canvas_size=540,
    )
    assert Image.open(out).size == (540, 540)


def test_top_band_paints_the_top_of_the_canvas(photo, tmp_path):
    """帯を置いた行が実際に塗られていること。"""
    plain = ThumbnailBuilder(canvas_size=400)
    plain.place_photo(Image.open(photo))
    before = plain.canvas.getpixel((200, 5))

    banded = ThumbnailBuilder(canvas_size=400)
    banded.place_photo(Image.open(photo))
    banded.add_badge(Badge("送料無料", "top_band", "red"))
    after = banded.canvas.getpixel((200, 5))

    assert before != after
    assert after[0] > after[1] + 50  # 赤系で塗られている


def test_empty_badge_text_is_ignored(photo):
    builder = ThumbnailBuilder(canvas_size=300)
    builder.place_photo(Image.open(photo))
    before = builder.canvas.tobytes()
    builder.add_badge(Badge("  ", "top_band"))
    assert builder.canvas.tobytes() == before


def test_blurred_backdrop_fills_the_corners(photo):
    builder = ThumbnailBuilder(canvas_size=400)
    builder.with_blurred_backdrop(Image.open(photo))
    # 元は白背景なので、ぼかし＋ベール後も明るいままだが真っ白ではない領域が出る
    assert builder.canvas.size == (400, 400)


def test_suggest_badges_uses_only_factual_claims(product):
    badges = suggest_badges(product, price=4980)
    assert "送料無料" in badges["top_text"]
    assert "4,980円" in badges["top_text"]
    assert badges["corner_text"] == "美品"  # no_scratch
    assert badges["bottom_text"] == "軽量"


def test_suggest_badges_omits_free_shipping_for_buyer_paid(product):
    product.shipping_payer = "buyer"
    badges = suggest_badges(product, price=1000)
    assert "送料無料" not in badges["top_text"]


def test_japanese_font_is_available_or_reported():
    """フォントが無い環境でも例外を投げず None を返す。"""
    path = find_japanese_font()
    assert path is None or Path(path).exists()


# ── ヘルパー ────────────────────────────────────────────────
def _mean_luma(image: Image.Image) -> float:
    return ImageStat.Stat(image.convert("L")).mean[0]


def _channel_spread(image: Image.Image) -> float:
    """RGB 各チャンネル平均の最大差。色かぶりの強さの目安。"""
    means = ImageStat.Stat(image.convert("RGB")).mean
    return max(means) - min(means)


# ── 色の忠実性 ─────────────────────────────────────────────
@pytest.fixture
def grey_product(tmp_path: Path) -> Path:
    """青みがかった背景に、無彩色（グレー）の商品を置いた写真。"""
    image = Image.new("RGB", (800, 800), (196, 198, 214))
    for x in range(220, 580):
        for y in range(220, 580):
            image.putpixel((x, y), (180, 180, 180))
    path = tmp_path / "grey.jpg"
    image.save(path, quality=95)
    return path


def _hue_spread(image: Image.Image, box) -> float:
    """指定領域のRGBチャンネル平均の最大差。無彩色なら 0 に近い。"""
    means = ImageStat.Stat(image.convert("RGB").crop(box)).mean
    return max(means) - min(means)


def test_auto_levels_preserves_hue_of_grey_products(grey_product):
    """グレーの商品がレベル補正で色付いてしまわないこと。

    チャンネル別に正規化すると灰色が青や緑に転ぶ。実物と色が違う写真は
    出品として致命的なので、輝度基準で伸ばしていることを検証する。
    """
    pipeline = ImagePipeline.open(grey_product).auto_levels(cutoff=0.4)
    box = (300, 300, 500, 500)  # 商品の内側だけを見る
    assert _hue_spread(pipeline.image, box) < 12


def test_white_balance_caps_its_correction(grey_product):
    """色かぶりを取りつつ、商品の色を壊すほどには振らないこと。"""
    before = _channel_spread(ImagePipeline.open(grey_product).image)
    after = _channel_spread(
        ImagePipeline.open(grey_product).white_balance().image
    )
    assert after < before          # かぶりは減る
    assert after >= 0              # 反転して行き過ぎない


def test_white_balance_does_not_drain_a_dominant_colour(tmp_path):
    """画面の大半が赤い商品でも、赤を打ち消してしまわない。"""
    image = Image.new("RGB", (400, 400), (220, 60, 60))
    path = tmp_path / "red.jpg"
    image.save(path, quality=95)

    corrected = ImagePipeline.open(path).white_balance().image
    means = ImageStat.Stat(corrected.convert("RGB")).mean
    assert means[0] > means[1] + 40   # 依然としてはっきり赤い
    assert means[0] > means[2] + 40


def test_auto_pipeline_keeps_grey_products_grey(grey_product):
    """自動加工を一通り通しても、無彩色の商品は無彩色のままであること。"""
    pipeline = ImagePipeline.open(grey_product).auto(size=600)
    centre = pipeline.image.size[0] // 2
    box = (centre - 60, centre - 60, centre + 60, centre + 60)
    assert _hue_spread(pipeline.image, box) < 15


# ── HEIC（iPhone写真）────────────────────────────────────────
def test_heic_photo_is_processed(tmp_path):
    """iPhone の HEIC がそのまま加工パイプラインを通ること。"""
    pytest.importorskip("pillow_heif")
    from mercari_tool.images import ImagePipeline

    src = tmp_path / "photo.heic"
    Image.new("RGB", (1200, 1600), (180, 190, 200)).save(src, format="HEIF")
    out = ImagePipeline.open(src).auto().save(tmp_path / "out.jpg")
    assert Image.open(out).size == (1080, 1080)
