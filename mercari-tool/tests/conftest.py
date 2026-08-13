from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mercari_tool.config import Config  # noqa: E402
from mercari_tool.models import Product, Supplier  # noqa: E402
from mercari_tool.pricing import FeeTable, PricingEngine  # noqa: E402


@pytest.fixture
def fees() -> FeeTable:
    return FeeTable.load()


@pytest.fixture
def engine(fees: FeeTable) -> PricingEngine:
    return PricingEngine(fees, target_margin=0.30, min_margin=0.12)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def product() -> Product:
    return Product(
        sku="TEST001",
        name="テスト商品",
        category="レディース/バッグ",
        brand="TestBrand",
        condition="no_scratch",
        shipping_method="nekopos",
        cost_price=1000,
        stock=1,
        keywords=["テスト", "バッグ"],
        selling_points=["軽量"],
        defects=["角に小傷あり"],
    )


@pytest.fixture
def supplier() -> Supplier:
    return Supplier(
        id="sup_test",
        name="テスト卸",
        channel="ネット卸",
        lead_time_days=5,
        min_order_qty=1,
        avg_unit_cost=1000,
        order_shipping_cost=500,
        defect_rate=0.02,
        reliability=4.0,
        order_count=10,
    )
