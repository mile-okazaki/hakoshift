"""出品ドラフトの組み立て。

1つの SKU に対して、リサーチ → 価格 → 画像 → 文章 を通しで実行し、
「あとは出品画面に貼るだけ」の状態まで持っていく。

自動投稿は行わない。メルカリには公開の出品APIが無く、
ボットによる自動出品は利用規約で禁止されているため、
最後の投稿操作は利用者が行う設計にしている。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..context import AppContext
from ..images import ImagePipeline, ThumbnailBuilder
from ..images.thumbnail import suggest_badges
from ..content import build_catchphrases, build_fallback_description, build_titles
from ..llm import LLMError
from ..models import ListingDraft, MarketStats, PriceRecommendation, Product
from ..research import analyze

#: メルカリに登録できる画像の上限
MAX_PHOTOS = 10
#: これを下回ると問い合わせが増えるので警告する枚数
RECOMMENDED_PHOTOS = 4


@dataclass
class DraftResult:
    """ドラフト生成の結果と、途中で起きたことの記録。"""

    draft: ListingDraft
    market: MarketStats
    price: PriceRecommendation
    #: 対応が要る問題（文字数超過、赤字、生成失敗など）
    warnings: list[str] = field(default_factory=list)
    #: 判断の材料になる補足（サンプル件数、相場の傾向など）
    notes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    output_dir: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft.to_dict(),
            "market": self.market.to_dict(),
            "price": self.price.to_dict(),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "steps": list(self.steps),
            "output_dir": str(self.output_dir) if self.output_dir else None,
        }


def _research_queries(product: Product) -> list[str]:
    """相場を探すときの検索語の候補を、具体的な順に並べる。"""
    parts = [product.brand.strip(), product.name.strip(), product.size_note.strip()]
    full = " ".join(p for p in parts if p)
    with_brand = " ".join(p for p in (product.brand.strip(), product.name.strip()) if p)

    candidates = [full, with_brand, product.name.strip()]
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


class DraftBuilder:
    """出品ドラフトを1本作る。"""

    def __init__(self, context: AppContext) -> None:
        self.ctx = context

    def build(
        self,
        product: Product,
        *,
        photos: Sequence[str | Path] = (),
        research_query: str | None = None,
        strategy: str = "",
        whiten_background: bool = False,
        generate_copy: bool = True,
        output_dir: str | Path | None = None,
    ) -> DraftResult:
        """SKU ひとつぶんのドラフトを作る。

        Args:
            product: 対象の商品
            photos: 出品に使う写真のパス。1枚目からサムネイルを作る。
            research_query: 相場検索の語。未指定なら商品名を使う。
            strategy: "quick" / "balanced" / "profit"。未指定なら推奨案。
            whiten_background: 写真の背景を白飛ばしするか
            generate_copy: Claude で文章を生成するか
        """
        warnings: list[str] = []
        notes: list[str] = []
        steps: list[str] = []

        target_dir = Path(output_dir) if output_dir else (
            self.ctx.config.output_dir / product.sku
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        # ── 1. リサーチ ────────────────────────────────────
        queries = (
            [research_query] if research_query else _research_queries(product)
        )
        market = self._research(queries, warnings, notes, steps)

        # ── 2. 価格 ────────────────────────────────────────
        recommendation = self.ctx.pricing.recommend(product, market)
        chosen = recommendation.recommended
        if strategy:
            for option in recommendation.options:
                if option.strategy == strategy:
                    chosen = option
                    break
            else:
                warnings.append(f"戦略 '{strategy}' が見つからないため推奨案を使いました。")
        price = chosen.price if chosen else 0
        for message in recommendation.notes:
            (warnings if message.startswith("⚠") else notes).append(message)
        steps.append(f"価格を決定: {price:,}円（{chosen.label if chosen else '-'}）")

        # ── 3. 画像 ────────────────────────────────────────
        image_paths, thumbnail_path = self._process_images(
            product, photos, price, target_dir, whiten_background, warnings, steps
        )

        # ── 4. 文章 ────────────────────────────────────────
        title = ""
        catchphrase = ""
        description = ""
        hashtags: list[str] = []
        alternatives: list[str] = []

        if generate_copy:
            title, catchphrase, description, hashtags, alternatives = self._generate_copy(
                product, market, warnings, steps
            )
        else:
            steps.append("Claude での文章生成はスキップしました（--no-copy）")

        if not title:
            # Claude を使わずに、商品情報と相場からタイトルを組み立てる
            built, catchphrases = build_titles(product, market)
            if built:
                title = built[0].text
                alternatives = [c.text for c in built[1:]]
                steps.append(f"タイトルを商品情報から組み立て: {len(built)}案")
            else:
                title = product.name[:40]
            catchphrase = catchphrase or (catchphrases[0] if catchphrases else "")

        if not catchphrase:
            phrases = build_catchphrases(product)
            catchphrase = phrases[0] if phrases else ""

        if not description:
            # 説明欄が空のまま出品作業に入らないよう、登録内容から組み立てる
            shipping_label = self.ctx.fees.shipping(product.shipping_method).label
            fallback = build_fallback_description(product, shipping_label)
            description = fallback.description
            hashtags = hashtags or fallback.hashtags
            warnings.extend(fallback.warnings)
            steps.append("説明文を商品情報から組み立て（テンプレート）")

        draft = ListingDraft(
            sku=product.sku,
            title=title,
            catchphrase=catchphrase,
            description=description,
            hashtags=hashtags,
            price=price,
            condition=product.condition,
            category=product.category,
            brand=product.brand,
            shipping_method=product.shipping_method,
            shipping_payer=product.shipping_payer,
            image_paths=[str(p) for p in image_paths],
            thumbnail_path=str(thumbnail_path) if thumbnail_path else "",
            title_alternatives=alternatives,
            price_recommendation=recommendation.to_dict(),
            market_stats=market.to_dict(),
        )

        return DraftResult(
            draft=draft,
            market=market,
            price=recommendation,
            warnings=warnings,
            notes=notes,
            steps=steps,
            output_dir=target_dir,
        )

    # ── 各工程 ─────────────────────────────────────────────
    def _research(
        self,
        queries: Sequence[str],
        warnings: list[str],
        notes: list[str],
        steps: list[str],
    ) -> MarketStats:
        """検索語の候補を順に試し、最初に見つかったデータを使う。

        買い手は「ナイキ エアマックス 90 27cm」のように打つが、
        商品名だけで保存していることもある。候補を広い順に試して
        どちらの置き方でも拾えるようにする。
        """
        last_error: Exception | None = None
        for query in queries:
            if not query:
                continue
            try:
                comps = self.ctx.research_provider.fetch(query, limit=200)
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                last_error = exc
                continue
            market = analyze(comps, query=query)
            steps.append(
                f"相場を取得: 「{query}」{market.sample_size}件 / "
                f"中央値 {market.median_price:,}円"
            )
            # 「⚠」付きは対応が要る問題、それ以外は判断材料
            for message in market.warnings:
                (warnings if message.startswith("⚠") else notes).append(message)
            return market

        tried = " / ".join(q for q in queries if q)
        warnings.append(
            f"相場データが見つかりませんでした（試した検索語: {tried}）。"
            f"{last_error if last_error else ''}"
        )
        steps.append("相場リサーチ: データなし")
        return MarketStats(query=queries[0] if queries else "")

    def _process_images(
        self,
        product: Product,
        photos: Sequence[str | Path],
        price: int,
        target_dir: Path,
        whiten: bool,
        warnings: list[str],
        steps: list[str],
    ) -> tuple[list[Path], Path | None]:
        if not photos:
            warnings.append(
                "写真が指定されていません。メルカリは画像1枚以上が必須です。"
                "--photos か --photos-dir で指定してください。"
            )
            steps.append("画像処理: 写真の指定なし")
            return [], None

        photos = list(photos)
        if len(photos) > MAX_PHOTOS:
            warnings.append(
                f"写真が {len(photos)} 枚あります。メルカリは最大 {MAX_PHOTOS} 枚なので、"
                f"先頭 {MAX_PHOTOS} 枚を使います。"
            )
            photos = photos[:MAX_PHOTOS]

        images_dir = target_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        processed: list[Path] = []

        for index, photo in enumerate(photos, start=1):
            try:
                pipeline = ImagePipeline.open(photo)
                pipeline.auto(whiten=whiten)
                saved = pipeline.save(images_dir / f"{product.sku}_{index:02d}.jpg")
                processed.append(saved)
            except (OSError, ValueError) as exc:
                warnings.append(f"画像の加工に失敗しました ({photo}): {exc}")

        if not processed:
            return [], None
        steps.append(f"画像を加工: {len(processed)}枚")
        if len(processed) < RECOMMENDED_PHOTOS:
            warnings.append(
                f"写真が {len(processed)} 枚です。"
                f"正面・背面・タグ・傷の箇所など {RECOMMENDED_PHOTOS} 枚以上あると"
                "問い合わせが減り、成約率が上がります。"
            )

        thumbnail_path: Path | None = None
        try:
            badges = suggest_badges(product, price)
            thumbnail_path = ThumbnailBuilder.build(
                processed[0],
                images_dir / f"{product.sku}_thumbnail.jpg",
                **badges,
            )
            steps.append("サムネイルを生成")
        except (OSError, ValueError) as exc:
            warnings.append(f"サムネイル生成に失敗しました: {exc}")

        return processed, thumbnail_path

    def _generate_copy(
        self,
        product: Product,
        market: MarketStats,
        warnings: list[str],
        steps: list[str],
    ) -> tuple[str, str, str, list[str], list[str]]:
        if not self.ctx.llm.is_available():
            # 空で返すことで、呼び出し元のルールベース組み立てに引き継ぐ
            steps.append("Claude 未設定のため、商品情報からの組み立てに切り替え")
            return "", "", "", [], []

        title = product.name[:40]
        catchphrase = ""
        alternatives: list[str] = []
        try:
            candidates, catchphrases = self.ctx.copywriter.generate_titles(
                product, market
            )
            if candidates:
                title = candidates[0].text
                alternatives = [c.text for c in candidates[1:]]
                for candidate in candidates:
                    warnings.extend(candidate.warnings)
            catchphrase = catchphrases[0] if catchphrases else ""
            steps.append(f"タイトルを生成: {len(candidates)}案")
        except LLMError as exc:
            warnings.append(f"タイトル生成に失敗しました: {exc}")

        description = ""
        hashtags: list[str] = []
        try:
            shipping_label = self.ctx.fees.shipping(product.shipping_method).label
            result = self.ctx.copywriter.generate_description(
                product, market, catchphrase=catchphrase, shipping_label=shipping_label
            )
            description = result.description
            hashtags = result.hashtags
            warnings.extend(result.warnings)
            steps.append(f"説明文を生成: {result.length}文字")
        except LLMError as exc:
            warnings.append(f"説明文の生成に失敗しました: {exc}")

        return title, catchphrase, description, hashtags, alternatives
