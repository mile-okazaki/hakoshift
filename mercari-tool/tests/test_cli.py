"""CLI の統合テスト。

各コマンドを実際に走らせ、データが期待どおり残ることまで見る。
Claude API は呼ばない（API キー未設定でも通る経路だけを使う）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from mercari_tool.cli import main
from mercari_tool.research import save_comps
from mercari_tool.models import SoldComp


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """CLI 用の作業ディレクトリ。環境変数の影響を受けないようにする。"""
    for name in (
        "ANTHROPIC_API_KEY",
        "MERCARI_DATA_DIR",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_SPREADSHEET_KEY",
        "MERCARI_ALLOW_HTTP_RESEARCH",
        "MERCARI_RESEARCH_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(workspace: Path, *args: str) -> int:
    return main(["--data-dir", str(workspace / "data"), *args])


# ── 基本 ───────────────────────────────────────────────────
def test_no_arguments_prints_help(capsys):
    assert main([]) == 1
    assert "mercari-tool" in capsys.readouterr().out


def test_init_creates_data_files(workspace, capsys):
    assert run(workspace, "init") == 0
    for name in ("products.json", "sales.json", "purchases.json", "suppliers.json"):
        assert (workspace / "data" / name).exists()


def test_fees_lists_shipping_options(workspace, capsys):
    assert run(workspace, "fees") == 0
    out = capsys.readouterr().out
    assert "ネコポス" in out
    assert "販売手数料" in out


# ── product ────────────────────────────────────────────────
def test_product_add_and_list(workspace, capsys):
    assert run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品", "--cost", "1200") == 0
    capsys.readouterr()
    assert run(workspace, "product", "list") == 0
    out = capsys.readouterr().out
    assert "A1" in out and "テスト品" in out


def test_product_add_is_idempotent_update(workspace, capsys):
    run(workspace, "product", "add", "--sku", "A1", "--name", "旧名")
    run(workspace, "product", "add", "--sku", "A1", "--name", "新名")
    capsys.readouterr()
    run(workspace, "product", "show", "A1")
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "新名"


def test_product_add_rejects_unknown_shipping_method(workspace, capsys):
    """存在しない配送区分は登録時点で弾き、使える値を示す。"""
    assert run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--shipping", "宇宙便") == 1
    err = capsys.readouterr().err
    assert "未知の配送方法" in err
    assert "nekopos" in err
    # 弾かれた商品が保存されていないこと
    assert not (workspace / "data" / "products.json").exists() or "A1" not in (
        workspace / "data" / "products.json"
    ).read_text(encoding="utf-8")


def test_product_show_reports_missing_sku(workspace, capsys):
    assert run(workspace, "product", "show", "NOPE") == 1
    assert "見つかりません" in capsys.readouterr().err


def test_product_add_parses_list_fields(workspace, capsys):
    run(
        workspace, "product", "add", "--sku", "A1", "--name", "x",
        "--keywords", "赤,Lサイズ", "--points", "軽い|丈夫", "--defects", "小傷|色あせ",
    )
    capsys.readouterr()
    run(workspace, "product", "show", "A1")
    data = json.loads(capsys.readouterr().out)
    assert data["keywords"] == ["赤", "Lサイズ"]
    assert data["selling_points"] == ["軽い", "丈夫"]
    assert data["defects"] == ["小傷", "色あせ"]


# ── research / price ───────────────────────────────────────
def _seed_comps(workspace: Path, query: str, prices: list[int]) -> None:
    comps = [SoldComp(title=query, price=p, sold=True) for p in prices]
    save_comps(workspace / "data" / "comps", query, comps)


def test_research_reads_saved_comps(workspace, capsys):
    run(workspace, "init")
    _seed_comps(workspace, "テスト品", [4000, 5000, 6000])
    capsys.readouterr()
    assert run(workspace, "research", "テスト品") == 0
    out = capsys.readouterr().out
    assert "中央値" in out and "5,000" in out


def test_research_without_data_fails_with_guidance(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "research", "存在しない品") == 1
    assert "相場データがありません" in capsys.readouterr().err


def test_price_suggest_uses_market_data(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品", "--cost", "1000")
    _seed_comps(workspace, "テスト品", [4800, 5000, 5200])
    capsys.readouterr()
    assert run(workspace, "price", "suggest", "A1") == 0
    out = capsys.readouterr().out
    assert "早く売る" in out and "バランス" in out and "利益重視" in out
    assert "損益分岐価格" in out


def test_price_suggest_json_is_machine_readable(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "1000")
    capsys.readouterr()
    run(workspace, "price", "suggest", "A1", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["recommended"]["price"] > 0
    assert len(data["options"]) == 3


def test_price_offer_declines_a_loss(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "3000")
    capsys.readouterr()
    run(workspace, "price", "offer", "A1", "500", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "decline"


def test_price_maxcost_reports_a_ceiling(workspace, capsys):
    assert run(workspace, "price", "maxcost", "--price", "5000", "--margin", "0.3") == 0
    assert "仕入れは" in capsys.readouterr().out


# ── image ──────────────────────────────────────────────────
@pytest.fixture
def photo(tmp_path: Path) -> Path:
    """被写体が十分大きい疑似写真。縮小の経路まで通るサイズにしてある。"""
    image = Image.new("RGB", (1400, 1800), (255, 255, 255))
    for x in range(200, 1200):
        for y in range(300, 1500):
            image.putpixel((x, y), (40, 90, 200))
    path = tmp_path / "photo.jpg"
    image.save(path)
    return path


def test_image_process_writes_square_output(workspace, photo, capsys):
    out = workspace / "edited"
    assert run(workspace, "image", "process", str(photo), "--out", str(out), "--size", "400") == 0
    files = list(out.glob("*.jpg"))
    assert len(files) == 1
    width, height = Image.open(files[0]).size
    assert width == height          # 正方形になっている
    assert width == 400             # 長辺は指定サイズまで縮小される


def test_image_process_does_not_upscale_small_photos(workspace, tmp_path, capsys):
    """画質が落ちるため、指定サイズより小さい写真は引き伸ばさない。"""
    small = tmp_path / "small.jpg"
    Image.new("RGB", (200, 200), (120, 120, 120)).save(small)
    out = workspace / "edited"
    assert run(workspace, "image", "process", str(small), "--out", str(out), "--size", "1080") == 0
    width, height = Image.open(next(out.glob("*.jpg"))).size
    assert width == height
    assert width <= 200


def test_image_thumb_writes_a_thumbnail(workspace, photo, capsys):
    out = workspace / "thumb.jpg"
    assert run(
        workspace, "image", "thumb", str(photo), "--out", str(out),
        "--top", "送料無料", "--size", "400",
    ) == 0
    assert Image.open(out).size == (400, 400)


# ── draft ──────────────────────────────────────────────────
def test_draft_runs_without_api_key(workspace, photo, capsys):
    """API キーが無くても、リサーチ・価格・画像までは通る。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品", "--cost", "1000")
    _seed_comps(workspace, "テスト品", [4800, 5000, 5200])
    capsys.readouterr()

    assert run(workspace, "draft", "A1", "--photos", str(photo), "--no-copy") == 0
    out = capsys.readouterr().out
    assert "出品ドラフト" in out

    target = workspace / "data" / "output" / "A1"
    assert (target / "listing.txt").exists()
    assert (target / "draft.json").exists()
    assert (target / "images" / "A1_thumbnail.jpg").exists()

    draft = json.loads((target / "draft.json").read_text(encoding="utf-8"))
    assert draft["draft"]["price"] > 0
    assert len(draft["draft"]["image_paths"]) == 1


def test_draft_listing_text_has_copy_paste_sections(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品", "--cost", "500")
    capsys.readouterr()
    run(workspace, "draft", "A1", "--no-copy")
    text = (workspace / "data" / "output" / "A1" / "listing.txt").read_text(encoding="utf-8")
    assert "① タイトル" in text
    assert "② 価格" in text
    assert "④ 商品の説明" in text
    assert "自動出品は利用規約で禁止" in text


def test_draft_respects_explicit_strategy(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品", "--cost", "1000")
    _seed_comps(workspace, "テスト品", [4800, 5000, 5200])
    capsys.readouterr()

    run(workspace, "draft", "A1", "--no-copy", "--strategy", "quick")
    quick = json.loads(
        (workspace / "data" / "output" / "A1" / "draft.json").read_text(encoding="utf-8")
    )["draft"]["price"]
    run(workspace, "draft", "A1", "--no-copy", "--strategy", "profit")
    profit = json.loads(
        (workspace / "data" / "output" / "A1" / "draft.json").read_text(encoding="utf-8")
    )["draft"]["price"]
    assert quick < profit


# ── comment ────────────────────────────────────────────────
def test_comment_generates_a_template_reply(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "1000")
    capsys.readouterr()
    assert run(workspace, "comment", "A1", "--text", "在庫はありますか", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["intent"] == "stock_check"
    assert data[0]["auto_send_ok"] is True


def test_comment_requires_input(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x")
    capsys.readouterr()
    assert run(workspace, "comment", "A1") == 1
    assert "コメントを指定" in capsys.readouterr().err


def test_comment_reads_a_batch_file(workspace, tmp_path, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "1000")
    batch = tmp_path / "comments.txt"
    batch.write_text("在庫ありますか\n発送はいつですか\n", encoding="utf-8")
    capsys.readouterr()
    run(workspace, "comment", "A1", "--file", str(batch), "--json")
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2


# ── supplier / sourcing ────────────────────────────────────
def test_supplier_add_and_rank(workspace, capsys):
    run(workspace, "init")
    run(workspace, "supplier", "add", "--id", "s1", "--name", "安い卸", "--unit-cost", "800",
        "--lead-time", "3", "--order-count", "20", "--reliability", "4.5")
    run(workspace, "supplier", "add", "--id", "s2", "--name", "高い卸", "--unit-cost", "2500",
        "--lead-time", "20", "--order-count", "1", "--reliability", "2.0")
    capsys.readouterr()

    assert run(workspace, "sourcing", "rank", "--price", "5000", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["supplier_name"] == "安い卸"
    assert data[0]["score"] > data[1]["score"]


def test_sourcing_rank_without_suppliers_fails(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "sourcing", "rank", "--price", "5000") == 1
    assert "仕入れ先が登録されていません" in capsys.readouterr().err


# ── sales ──────────────────────────────────────────────────
def test_sales_add_buy_and_report(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品",
        "--cost", "1000", "--category", "本")
    run(workspace, "sales", "buy", "--sku", "A1", "--cost", "1000", "--qty", "3",
        "--date", "2026-05-01")
    run(workspace, "sales", "add", "--sku", "A1", "--price", "5000", "--date", "2026-05-10")
    run(workspace, "sales", "add", "--sku", "A1", "--price", "4000", "--date", "2026-06-10")
    capsys.readouterr()

    assert run(workspace, "sales", "report", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["monthly"]) == 2
    assert data["overall"]["revenue"] == 9000
    assert data["overall"]["purchase_amount"] == 3000
    # 純利益 = 売上 − (原価 + 手数料 + 送料 + 梱包 + その他)
    assert data["overall"]["net_profit"] == 9000 - data["overall"]["total_expense"]
    assert data["by_category"][0]["label"] == "本"


def test_sales_report_shows_cash_flow_separately(workspace, capsys):
    """仕入れが多い月は、黒字でも現金収支がマイナスになることを表示できる。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "1000")
    run(workspace, "sales", "buy", "--sku", "A1", "--cost", "1000", "--qty", "30",
        "--date", "2026-05-01")
    run(workspace, "sales", "add", "--sku", "A1", "--price", "5000", "--date", "2026-05-10")
    capsys.readouterr()

    run(workspace, "sales", "report", "--json")
    month = json.loads(capsys.readouterr().out)["monthly"][0]
    assert month["net_profit"] > 0
    assert month["cash_flow"] < 0


def test_sales_report_filters_by_date_range(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "100")
    run(workspace, "sales", "add", "--sku", "A1", "--price", "1000", "--date", "2026-01-10")
    run(workspace, "sales", "add", "--sku", "A1", "--price", "2000", "--date", "2026-03-10")
    capsys.readouterr()

    run(workspace, "sales", "report", "--start", "2026-03-01", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["overall"]["revenue"] == 2000


def test_sales_report_with_no_data(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "sales", "report") == 0
    assert "データがありません" in capsys.readouterr().out


def test_sales_export_and_import_roundtrip(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "1000")
    run(workspace, "sales", "add", "--sku", "A1", "--price", "5000", "--date", "2026-05-10")
    out = workspace / "sales.csv"
    capsys.readouterr()
    assert run(workspace, "sales", "export", "--out", str(out)) == 0
    assert out.exists()
    assert "純利益" in out.read_text(encoding="utf-8-sig")


def test_sales_import_csv(workspace, tmp_path, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x", "--cost", "500")
    source = tmp_path / "in.csv"
    source.write_text("sku,sale_price,sold_at\nA1,3000,2026-07-01\n", encoding="utf-8")
    capsys.readouterr()
    assert run(workspace, "sales", "import", str(source)) == 0
    assert "1 件" in capsys.readouterr().out


def test_sales_sync_without_credentials_reports_clearly(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "sales", "sync") == 1
    err = capsys.readouterr().err
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in err or "gspread" in err


def test_invalid_date_is_rejected(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x")
    capsys.readouterr()
    with pytest.raises(Exception):
        run(workspace, "sales", "add", "--sku", "A1", "--price", "1000", "--date", "昨日")


def test_draft_without_api_key_still_writes_a_description(workspace, capsys):
    """API キーが無くても、説明欄が空のまま出力されない。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品", "--cost", "500",
        "--brand", "TestBrand", "--defects", "小傷あり", "--points", "軽量です")
    capsys.readouterr()
    run(workspace, "draft", "A1", "--no-copy")

    text = (workspace / "data" / "output" / "A1" / "listing.txt").read_text(encoding="utf-8")
    assert "【商品の詳細】" in text
    assert "小傷あり" in text          # 申告した難点が載っている
    assert "#TestBrand" in text
