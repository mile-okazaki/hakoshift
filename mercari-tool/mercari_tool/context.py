"""アプリ全体の組み立て。

各コンポーネントの依存関係をここ1か所にまとめ、
CLI からもスクリプトからも同じ形で使えるようにする。
"""

from __future__ import annotations

from functools import cached_property

from .config import Config
from .content import CommentResponder, ListingCopyGenerator
from .llm import LLMClient
from .pricing import FeeTable, PricingEngine
from .research import build_provider
from .research.providers import MarketDataProvider
from .sales import Ledger
from .sourcing import SupplierEvaluator
from .storage import JsonCollection
from .models import Supplier


class AppContext:
    """設定から各機能を組み立てて保持する。

    重い初期化（手数料テーブル読み込み、API クライアント生成）は
    実際に使われるまで遅延させる。
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.load()
        self.config.ensure_dirs()

    @cached_property
    def fees(self) -> FeeTable:
        return FeeTable.load(self.config.fees_path)

    @cached_property
    def pricing(self) -> PricingEngine:
        return PricingEngine(
            self.fees,
            target_margin=self.config.target_margin,
            min_margin=self.config.min_margin,
        )

    @cached_property
    def ledger(self) -> Ledger:
        return Ledger(self.config, fees=self.fees)

    @cached_property
    def suppliers(self) -> JsonCollection[Supplier]:
        return JsonCollection(
            self.config.suppliers_path, Supplier.from_dict, key="id"
        )

    @cached_property
    def llm(self) -> LLMClient:
        return LLMClient(self.config)

    @cached_property
    def copywriter(self) -> ListingCopyGenerator:
        return ListingCopyGenerator(self.llm)

    @cached_property
    def responder(self) -> CommentResponder:
        return CommentResponder(llm=self.llm, pricing=self.pricing)

    @cached_property
    def evaluator(self) -> SupplierEvaluator:
        return SupplierEvaluator(self.pricing)

    @cached_property
    def research_provider(self) -> MarketDataProvider:
        return build_provider(self.config)
