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
    err = capsys.readouterr().err
    assert "相場データが見つかりませんでした" in err
    assert "research add" in err          # 次にやることが書いてある


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


# ── research add ───────────────────────────────────────────
def test_research_add_stores_pasted_prices(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "research", "add", "テスト品", "--prices", "4000,5000,6000") == 0
    out = capsys.readouterr().out
    assert "追加 3件" in out
    assert "5,000" in out          # そのまま集計まで出る

    # 保存した相場が research からそのまま読めること
    assert run(workspace, "research", "テスト品") == 0
    assert "中央値" in capsys.readouterr().out


def test_research_add_reads_lines_from_a_file(workspace, capsys):
    run(workspace, "init")
    source = workspace / "prices.txt"
    source.write_text(
        "エアマックス 90 27cm 11500 売切\n美品 9800円\n出品中 16800\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert run(workspace, "research", "add", "テスト品", "--file", str(source)) == 0
    out = capsys.readouterr().out
    assert "追加 3件" in out
    assert "売却済み 2" in out      # 出品中の1件は売却扱いにしない


def test_research_add_reports_lines_it_could_not_read(workspace, capsys):
    run(workspace, "init")
    source = workspace / "prices.txt"
    source.write_text("9800\n値段のない行\n", encoding="utf-8")
    capsys.readouterr()
    assert run(workspace, "research", "add", "テスト品", "--file", str(source)) == 0
    out = capsys.readouterr().out
    assert "追加 1件" in out
    assert "読めなかった行" in out


def test_research_add_accumulates_across_runs(workspace, capsys):
    run(workspace, "init")
    run(workspace, "research", "add", "テスト品", "--prices", "4000,5000", "--quiet")
    capsys.readouterr()
    run(workspace, "research", "add", "テスト品", "--prices", "5000,6000", "--quiet")
    out = capsys.readouterr().out
    assert "追加 1件" in out and "重複スキップ 1件" in out and "合計 3件" in out


def test_research_add_replace_starts_over(workspace, capsys):
    run(workspace, "init")
    run(workspace, "research", "add", "テスト品", "--prices", "4000,5000", "--quiet")
    capsys.readouterr()
    run(workspace, "research", "add", "テスト品", "--prices", "9000", "--replace", "--quiet")
    assert "合計 1件" in capsys.readouterr().out


def test_research_add_uses_the_query_draft_will_look_up(workspace, capsys):
    """--sku で貯めた相場が、そのまま draft から拾われること。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "エアマックス 90",
        "--brand", "ナイキ", "--size", "27cm", "--cost", "4000")
    run(workspace, "research", "add", "--sku", "A1", "--prices", "9800,11500,10200", "--quiet")
    capsys.readouterr()
    run(workspace, "draft", "A1", "--no-copy")
    assert "相場を取得" in capsys.readouterr().out


def test_research_add_without_any_price_fails(workspace, capsys):
    run(workspace, "init")
    source = workspace / "prices.txt"
    source.write_text("読めない行だけ\n", encoding="utf-8")
    capsys.readouterr()
    assert run(workspace, "research", "add", "テスト品", "--file", str(source)) == 1
    assert "読み取れませんでした" in capsys.readouterr().err


def test_research_add_needs_a_query(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "research", "add", "--prices", "1000") == 1


# ── product import ─────────────────────────────────────────
def _shiire_csv(workspace: Path) -> Path:
    path = workspace / "shiire.csv"
    path.write_text(
        "sku,商品名,ブランド,状態,サイズ,仕入値,在庫,配送方法,キーワード,訴求ポイント\n"
        "A1,エアマックス 90,ナイキ,美品,27cm,4500,1,size80,スニーカー|ホワイト,箱付き\n"
        "B2,フリース,ユニクロ,新品,M,1200,3,nekopos,防寒,タグ付き\n",
        encoding="utf-8",
    )
    return path


def test_product_import_registers_every_row(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "product", "import", str(_shiire_csv(workspace))) == 0
    assert "新規 2件" in capsys.readouterr().out

    run(workspace, "product", "show", "A1")
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "エアマックス 90"
    assert data["condition"] == "no_scratch"       # 「美品」を内部キーに直す
    assert data["keywords"] == ["スニーカー", "ホワイト"]
    assert data["cost_price"] == 4500


def test_product_import_dry_run_saves_nothing(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    run(workspace, "product", "import", str(_shiire_csv(workspace)), "--dry-run")
    assert "確認のみ" in capsys.readouterr().out
    assert run(workspace, "product", "show", "A1") == 1


def test_product_import_updates_without_clearing_other_fields(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "import", str(_shiire_csv(workspace)))
    stock_only = workspace / "stock.csv"
    stock_only.write_text("sku,在庫\nA1,5\n", encoding="utf-8")
    capsys.readouterr()
    run(workspace, "product", "import", str(stock_only))
    assert "更新 1件" in capsys.readouterr().out

    run(workspace, "product", "show", "A1")
    data = json.loads(capsys.readouterr().out)
    assert data["stock"] == 5
    assert data["keywords"] == ["スニーカー", "ホワイト"]   # 消えていない


def test_product_import_rejects_an_unknown_shipping_method(workspace, capsys):
    run(workspace, "init")
    path = workspace / "ng.csv"
    path.write_text("sku,商品名,配送方法\nA1,テスト,宇宙便\n", encoding="utf-8")
    capsys.readouterr()
    run(workspace, "product", "import", str(path))
    assert "配送方法" in capsys.readouterr().out
    assert run(workspace, "product", "show", "A1") == 1


def test_product_import_template_can_be_imported_back(workspace, capsys):
    run(workspace, "init")
    template = workspace / "template.csv"
    capsys.readouterr()
    assert run(workspace, "product", "import", "--template", str(template)) == 0
    assert template.exists()
    capsys.readouterr()
    assert run(workspace, "product", "import", str(template)) == 0
    assert "新規 1件" in capsys.readouterr().out


def test_product_import_without_a_path_fails(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "product", "import") == 1


# ── draft をまとめて ────────────────────────────────────────
def _photo_tree(workspace: Path, skus: dict[str, int]) -> Path:
    root = workspace / "photos"
    for sku, count in skus.items():
        folder = root / sku
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            Image.new("RGB", (1200, 1500), (180, 190, 200)).save(
                folder / f"{index:02d}_shot.jpg"
            )
    return root


def test_draft_all_generates_every_product(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "import", str(_shiire_csv(workspace)))
    root = _photo_tree(workspace, {"A1": 4, "B2": 4})
    capsys.readouterr()

    assert run(workspace, "draft", "--all", "--photos-root", str(root), "--no-copy") == 0
    out = capsys.readouterr().out
    assert "生成 2件" in out
    for sku in ("A1", "B2"):
        target = workspace / "data" / "output" / sku
        assert (target / "listing.txt").exists()
        assert (target / "images" / f"{sku}_thumbnail.jpg").exists()
    assert (workspace / "data" / "output" / "drafts.csv").exists()


def test_draft_all_writes_one_csv_row_per_sku(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "import", str(_shiire_csv(workspace)))
    capsys.readouterr()
    run(workspace, "draft", "--all", "--no-copy")
    rows = (workspace / "data" / "output" / "drafts.csv").read_text(
        encoding="utf-8-sig"
    ).splitlines()
    assert rows[0].startswith("sku,")
    assert any(line.startswith("A1,") for line in rows)
    assert any(line.startswith("B2,") for line in rows)


def test_draft_accepts_several_skus(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "import", str(_shiire_csv(workspace)))
    capsys.readouterr()
    assert run(workspace, "draft", "A1", "B2", "--no-copy") == 0
    assert "生成 2件" in capsys.readouterr().out


def test_draft_in_stock_skips_sold_out_products(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "在庫あり", "--stock", "2")
    run(workspace, "product", "add", "--sku", "B2", "--name", "在庫なし", "--stock", "0")
    capsys.readouterr()
    run(workspace, "draft", "--all", "--in-stock", "--no-copy")
    out = capsys.readouterr().out
    assert "A1" in out and "B2" not in out


def test_draft_photos_root_points_at_the_missing_folder(workspace, capsys):
    """写真が置かれていないとき、どこに置けばよいかを示す。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "写真なし")
    run(workspace, "product", "add", "--sku", "B2", "--name", "写真なし2")
    root = _photo_tree(workspace, {"A1": 1})
    capsys.readouterr()
    run(workspace, "draft", "--all", "--photos-root", str(root), "--no-copy")
    out = capsys.readouterr().out
    assert str(root / "B2") in out


def test_draft_photos_root_accepts_flat_files(workspace, capsys):
    """photos/A1_01.jpg のような平置きでも拾う。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "テスト品")
    root = workspace / "flat"
    root.mkdir()
    for index in (1, 2):
        Image.new("RGB", (1200, 1500), (200, 200, 200)).save(root / f"A1_{index:02d}.jpg")
    capsys.readouterr()
    run(workspace, "draft", "A1", "--photos-root", str(root), "--no-copy")
    draft = json.loads(
        (workspace / "data" / "output" / "A1" / "draft.json").read_text(encoding="utf-8")
    )
    assert len(draft["draft"]["image_paths"]) == 2


def test_draft_needs_a_target(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "draft", "--no-copy") == 1
    assert "--all" in capsys.readouterr().err


def test_draft_all_and_sku_together_is_rejected(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "x")
    capsys.readouterr()
    assert run(workspace, "draft", "A1", "--all", "--no-copy") == 1


def test_draft_out_is_refused_for_multiple_skus(workspace, capsys):
    """複数点を1つのフォルダに書くと上書きし合うので断る。"""
    run(workspace, "init")
    run(workspace, "product", "import", str(_shiire_csv(workspace)))
    capsys.readouterr()
    assert run(workspace, "draft", "A1", "B2", "--out", str(workspace / "o"), "--no-copy") == 1


def test_draft_all_with_no_products_is_not_an_error(workspace, capsys):
    run(workspace, "init")
    capsys.readouterr()
    assert run(workspace, "draft", "--all", "--no-copy") == 0
    assert "対象の商品がありません" in capsys.readouterr().out


def test_draft_all_with_a_single_product_still_writes_the_csv(workspace, capsys):
    """--all は対象が1点でも一括モードとして振る舞う。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "一点だけ")
    capsys.readouterr()
    assert run(workspace, "draft", "--all", "--no-copy") == 0
    out = capsys.readouterr().out
    assert "生成 1件" in out
    assert (workspace / "data" / "output" / "drafts.csv").exists()


def test_research_add_then_stats_sees_a_preexisting_csv_too(workspace, capsys):
    """手置きCSVがある検索語に追記しても、両方のデータが集計に入る。"""
    run(workspace, "init")
    from mercari_tool.research import slugify
    csv_path = workspace / "data" / "comps" / f"{slugify('テスト品')}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("商品名,価格\n白,4000\n黒,5000\n", encoding="utf-8")
    capsys.readouterr()
    run(workspace, "research", "add", "テスト品", "--prices", "6000", "--quiet")
    assert "合計 3件" in capsys.readouterr().out
    capsys.readouterr()
    assert run(workspace, "research", "テスト品") == 0
    assert "サンプル       : 3 件" in capsys.readouterr().out


# ── レビューで見つかった統合バグの回帰テスト ─────────────────────
def test_flat_photos_do_not_leak_between_prefix_sharing_skus(workspace, capsys):
    """photos/A-1_01.jpg の検索に A-10 の写真が混ざらない。"""
    from mercari_tool.cli import photos_for_sku

    root = workspace / "flat"
    root.mkdir()
    for name in ("A-1_01.jpg", "A-10_01.jpg", "A-10_02.jpg", "A-1.jpg"):
        Image.new("RGB", (600, 600), (200, 200, 200)).save(root / name)
    found = [Path(p).name for p in photos_for_sku(root, "A-1")]
    assert found == ["A-1.jpg", "A-1_01.jpg"]


def test_rejected_import_row_does_not_leak_into_the_ledger(workspace, capsys):
    """配送方法が不正で弾いた行の内容が、他の行の保存に紛れて書き込まれない。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "正しい名前", "--cost", "1000")
    bad = workspace / "bad.csv"
    bad.write_text(
        "sku,商品名,仕入値,配送方法\n"
        "A1,汚染された名前,9999,宇宙便\n"      # 弾かれるべき行
        "NEW1,新規品,500,nekopos\n",           # 正常な行（保存が走る）
        encoding="utf-8",
    )
    capsys.readouterr()
    run(workspace, "product", "import", str(bad))
    capsys.readouterr()
    run(workspace, "product", "show", "A1")
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "正しい名前"        # 弾いた行の内容が漏れていない
    assert data["cost_price"] == 1000


def test_draft_all_continues_past_a_broken_shipping_method(workspace, capsys):
    """1点の配送区分が壊れていても、残りの生成と一覧CSVは完走する。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "OK1", "--name", "正常品")
    run(workspace, "product", "add", "--sku", "NG1", "--name", "壊れた品")
    run(workspace, "product", "add", "--sku", "OK2", "--name", "正常品2")
    # 登録後に fees.json のキーずれを模擬（README が利用者更新を指示している）
    products_path = workspace / "data" / "products.json"
    data = json.loads(products_path.read_text(encoding="utf-8"))
    for item in data:
        if item["sku"] == "NG1":
            item["shipping_method"] = "size80_typo"
    products_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()

    assert run(workspace, "draft", "--all", "--no-copy") == 0
    out = capsys.readouterr().out
    assert "✖ NG1" in out
    assert "生成 2件 / 失敗 1件" in out
    assert (workspace / "data" / "output" / "OK1" / "listing.txt").exists()
    assert (workspace / "data" / "output" / "OK2" / "listing.txt").exists()
    assert (workspace / "data" / "output" / "drafts.csv").exists()


def test_research_add_sku_is_found_by_price_suggest(workspace, capsys):
    """--sku で書き写した相場が price suggest からそのまま使われる。"""
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "エアマックス 90",
        "--brand", "ナイキ", "--size", "27cm", "--cost", "4000")
    run(workspace, "research", "add", "--sku", "A1",
        "--prices", "9800,11500,10200,10000", "--quiet")
    capsys.readouterr()
    assert run(workspace, "price", "suggest", "A1") == 0
    out = capsys.readouterr().out
    assert "相場中央値" in out                 # コスト逆算ではなく相場ベース


def test_research_add_sku_is_found_by_research_stats(workspace, capsys):
    run(workspace, "init")
    run(workspace, "product", "add", "--sku", "A1", "--name", "エアマックス 90",
        "--brand", "ナイキ", "--size", "27cm")
    run(workspace, "research", "add", "--sku", "A1", "--prices", "9800,11500", "--quiet")
    capsys.readouterr()
    assert run(workspace, "research", "--sku", "A1") == 0
    assert "中央値" in capsys.readouterr().out
