"""Google スプレッドシートへの反映。

売上を記録するたび、または `sales sync` を叩くたびに、
明細・日次推移・月次推移・カテゴリ別・在庫・サマリーの各シートを作り直す。

差分更新ではなく毎回上書きにしているのは、
台帳（JSON）を唯一の正とし、シート側での手編集とズレないようにするため。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

from ..config import Config
from ..models import Purchase, Sale
from .analytics import (
    GroupSummary,
    PeriodSummary,
    daily_summary,
    group_by,
    monthly_summary,
    overall_summary,
)
from .ledger import Ledger

#: 書き出すシートの名前
SHEET_SUMMARY = "サマリー"
SHEET_DAILY = "日次推移"
SHEET_MONTHLY = "月次推移"
SHEET_SALES = "売上明細"
SHEET_PURCHASES = "仕入明細"
SHEET_CATEGORY = "カテゴリ別"
SHEET_INVENTORY = "在庫"

_YEN = "¥#,##0"
_PERCENT = "0.0%"


class SheetsError(RuntimeError):
    """スプレッドシート連携の失敗。"""


class SheetsSync:
    """台帳の内容を Google スプレッドシートに反映する。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Any = None
        self._spreadsheet: Any = None

    # ── 接続 ──────────────────────────────────────────────
    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise SheetsError(
                "スプレッドシート連携には gspread が必要です:\n"
                "  pip install gspread google-auth"
            ) from exc

        key_path = self.config.google_service_account_json
        if not key_path:
            raise SheetsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。\n"
                "Google Cloud でサービスアカウントを作り、JSONキーのパスを .env に設定してください。"
            )
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        try:
            credentials = Credentials.from_service_account_file(key_path, scopes=scopes)
        except (OSError, ValueError) as exc:
            raise SheetsError(f"サービスアカウントのJSONを読めません: {key_path}\n{exc}") from exc
        self._client = gspread.authorize(credentials)
        return self._client

    def _open(self) -> Any:
        """対象のスプレッドシートを開く。無ければ作る。"""
        if self._spreadsheet is not None:
            return self._spreadsheet
        client = self._connect()
        import gspread

        if self.config.google_spreadsheet_key:
            try:
                self._spreadsheet = client.open_by_key(self.config.google_spreadsheet_key)
            except gspread.SpreadsheetNotFound as exc:
                raise SheetsError(
                    f"スプレッドシートが見つかりません: {self.config.google_spreadsheet_key}\n"
                    "サービスアカウントのメールアドレスに編集権限を共有してください。"
                ) from exc
        else:
            name = self.config.google_spreadsheet_name
            try:
                self._spreadsheet = client.open(name)
            except gspread.SpreadsheetNotFound:
                self._spreadsheet = client.create(name)
        return self._spreadsheet

    @property
    def url(self) -> str:
        return self._open().url

    # ── シート操作 ──────────────────────────────────────────
    def _worksheet(self, title: str, rows: int, cols: int) -> Any:
        spreadsheet = self._open()
        import gspread

        try:
            worksheet = spreadsheet.worksheet(title)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=title, rows=max(rows, 20), cols=max(cols, 10)
            )
        return worksheet

    @staticmethod
    def _write(worksheet: Any, values: list[list[Any]]) -> None:
        if not values:
            return
        # gspread 5.x / 6.x のどちらでも通るキーワード呼び出し
        worksheet.update(range_name="A1", values=values)

    def _style(
        self,
        worksheet: Any,
        column_count: int,
        yen_columns: Sequence[str] = (),
        percent_columns: Sequence[str] = (),
        row_count: int = 1000,
    ) -> None:
        """ヘッダー装飾と数値書式。失敗しても同期自体は止めない。"""
        try:
            worksheet.format(
                f"A1:{_col_letter(column_count)}1",
                {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.96},
                    "horizontalAlignment": "CENTER",
                },
            )
            worksheet.freeze(rows=1)
            for column in yen_columns:
                worksheet.format(
                    f"{column}2:{column}{row_count}",
                    {"numberFormat": {"type": "NUMBER", "pattern": _YEN}},
                )
            for column in percent_columns:
                worksheet.format(
                    f"{column}2:{column}{row_count}",
                    {"numberFormat": {"type": "NUMBER", "pattern": _PERCENT}},
                )
        except Exception:  # noqa: BLE001 - 書式は付加価値なので失敗を致命にしない
            pass

    # ── 同期本体 ────────────────────────────────────────────
    def sync(self, ledger: Ledger) -> dict[str, Any]:
        """台帳の全内容をシートに書き出す。"""
        sales = sorted(ledger.sales.all(), key=lambda s: s.sold_at or date.min)
        purchases = sorted(
            ledger.purchases.all(), key=lambda p: p.purchased_at or date.min
        )
        supplier_labels = {
            p.supplier_id: p.supplier_id for p in purchases if p.supplier_id
        }

        self._sync_sales(sales)
        self._sync_purchases(purchases)
        daily = daily_summary(sales, purchases)
        monthly = monthly_summary(sales, purchases)
        self._sync_period(SHEET_DAILY, daily, "日付")
        self._sync_period(SHEET_MONTHLY, monthly, "年月")
        self._sync_category(group_by(sales, "category"))
        self._sync_inventory(ledger.inventory())
        total = overall_summary(sales, purchases)
        self._sync_summary(total, monthly, supplier_labels)

        return {
            "url": self.url,
            "sales_rows": len(sales),
            "purchase_rows": len(purchases),
            "daily_rows": len(daily),
            "monthly_rows": len(monthly),
        }

    # ── 各シート ────────────────────────────────────────────
    def _sync_sales(self, sales: Sequence[Sale]) -> None:
        header = [
            "売却日", "出品日", "日数", "SKU", "商品名", "カテゴリ", "仕入先",
            "販売価格", "仕入値", "販売手数料", "送料", "梱包費", "その他",
            "費用合計", "純利益", "利益率", "メモ",
        ]
        rows: list[list[Any]] = [header]
        for sale in sales:
            rows.append(
                [
                    _fmt_date(sale.sold_at),
                    _fmt_date(sale.listed_at),
                    sale.days_to_sell if sale.days_to_sell is not None else "",
                    sale.sku,
                    sale.product_name,
                    sale.category,
                    sale.supplier_id,
                    sale.sale_price,
                    sale.cost_price,
                    sale.commission,
                    sale.shipping_cost,
                    sale.packaging_cost,
                    sale.other_cost,
                    sale.total_cost,
                    sale.net_profit,
                    round(sale.margin_rate, 4),
                    sale.memo,
                ]
            )
        worksheet = self._worksheet(SHEET_SALES, len(rows) + 10, len(header))
        self._write(worksheet, rows)
        self._style(
            worksheet,
            len(header),
            yen_columns=("H", "I", "J", "K", "L", "M", "N", "O"),
            percent_columns=("P",),
            row_count=len(rows) + 1,
        )

    def _sync_purchases(self, purchases: Sequence[Purchase]) -> None:
        header = [
            "仕入日", "SKU", "商品名", "仕入先", "単価", "数量",
            "送料", "その他", "仕入合計", "実質単価", "メモ",
        ]
        rows: list[list[Any]] = [header]
        for purchase in purchases:
            rows.append(
                [
                    _fmt_date(purchase.purchased_at),
                    purchase.sku,
                    purchase.product_name,
                    purchase.supplier_id,
                    purchase.unit_cost,
                    purchase.quantity,
                    purchase.shipping_cost,
                    purchase.other_cost,
                    purchase.total_cost,
                    purchase.unit_cost_landed,
                    purchase.memo,
                ]
            )
        worksheet = self._worksheet(SHEET_PURCHASES, len(rows) + 10, len(header))
        self._write(worksheet, rows)
        self._style(
            worksheet,
            len(header),
            yen_columns=("E", "G", "H", "I", "J"),
            row_count=len(rows) + 1,
        )

    def _sync_period(
        self, title: str, rows_data: Sequence[PeriodSummary], period_label: str
    ) -> None:
        header = [
            period_label, "販売件数", "売上", "売上原価", "販売手数料", "送料",
            "梱包費", "その他", "費用合計", "純利益", "利益率",
            "平均単価", "仕入支払額", "現金収支",
        ]
        rows: list[list[Any]] = [header]
        for row in rows_data:
            rows.append(
                [
                    row.period,
                    row.sales_count,
                    row.revenue,
                    row.cogs,
                    row.commission,
                    row.shipping,
                    row.packaging,
                    row.other,
                    row.total_expense,
                    row.net_profit,
                    round(row.margin_rate, 4),
                    row.avg_sale_price,
                    row.purchase_amount,
                    row.cash_flow,
                ]
            )
        worksheet = self._worksheet(title, len(rows) + 10, len(header))
        self._write(worksheet, rows)
        self._style(
            worksheet,
            len(header),
            yen_columns=("C", "D", "E", "F", "G", "H", "I", "J", "L", "M", "N"),
            percent_columns=("K",),
            row_count=len(rows) + 1,
        )

    def _sync_category(self, groups: Sequence[GroupSummary]) -> None:
        header = ["カテゴリ", "販売件数", "売上", "売上原価", "純利益", "利益率", "ROI", "平均売却日数"]
        rows: list[list[Any]] = [header]
        for group in groups:
            rows.append(
                [
                    group.label,
                    group.sales_count,
                    group.revenue,
                    group.cogs,
                    group.net_profit,
                    round(group.margin_rate, 4),
                    round(group.roi, 4),
                    group.avg_days_to_sell if group.avg_days_to_sell is not None else "",
                ]
            )
        worksheet = self._worksheet(SHEET_CATEGORY, len(rows) + 10, len(header))
        self._write(worksheet, rows)
        self._style(
            worksheet,
            len(header),
            yen_columns=("C", "D", "E"),
            percent_columns=("F", "G"),
            row_count=len(rows) + 1,
        )

    def _sync_inventory(self, inventory: Sequence[dict[str, Any]]) -> None:
        header = ["SKU", "商品名", "在庫数", "単価", "寝ている金額", "仕入先"]
        rows: list[list[Any]] = [header]
        for item in inventory:
            rows.append(
                [
                    item["sku"],
                    item["name"],
                    item["stock"],
                    item["unit_cost"],
                    item["tied_up"],
                    item["supplier_id"],
                ]
            )
        worksheet = self._worksheet(SHEET_INVENTORY, len(rows) + 10, len(header))
        self._write(worksheet, rows)
        self._style(
            worksheet, len(header), yen_columns=("D", "E"), row_count=len(rows) + 1
        )

    def _sync_summary(
        self,
        total: PeriodSummary,
        monthly: Sequence[PeriodSummary],
        supplier_labels: dict[str, str],
    ) -> None:
        latest = monthly[-1] if monthly else PeriodSummary(period="-")
        rows: list[list[Any]] = [
            ["メルカリ売上サマリー", ""],
            ["最終更新", datetime.now().strftime("%Y-%m-%d %H:%M")],
            ["", ""],
            ["■ 全期間", ""],
            ["販売件数", total.sales_count],
            ["売上", total.revenue],
            ["売上原価（仕入）", total.cogs],
            ["販売手数料", total.commission],
            ["送料", total.shipping],
            ["梱包費・その他", total.packaging + total.other],
            ["費用合計", total.total_expense],
            ["純利益", total.net_profit],
            ["利益率", round(total.margin_rate, 4)],
            ["平均販売単価", total.avg_sale_price],
            ["", ""],
            ["■ 直近月", latest.period],
            ["販売件数", latest.sales_count],
            ["売上", latest.revenue],
            ["純利益", latest.net_profit],
            ["利益率", round(latest.margin_rate, 4)],
            ["仕入支払額", latest.purchase_amount],
            ["現金収支", latest.cash_flow],
            ["", ""],
            ["■ 仕入と売上の差引", ""],
            ["支払った仕入額（全期間）", total.purchase_amount],
            ["受け取った売上（全期間）", total.revenue],
            ["差引（現金収支）", total.cash_flow],
            ["", ""],
            ["※ 純利益 = 売上 −（売れた商品の仕入値＋手数料＋送料＋梱包費）", ""],
            ["※ 現金収支 = 売上 −（手数料＋送料＋梱包費）− その期間に支払った仕入額", ""],
        ]
        worksheet = self._worksheet(SHEET_SUMMARY, len(rows) + 10, 2)
        self._write(worksheet, rows)
        try:
            worksheet.format("A1:B1", {"textFormat": {"bold": True, "fontSize": 14}})
            worksheet.format(
                "B5:B30", {"numberFormat": {"type": "NUMBER", "pattern": _YEN}}
            )
            worksheet.format(
                "B13", {"numberFormat": {"type": "NUMBER", "pattern": _PERCENT}}
            )
            worksheet.format(
                "B20", {"numberFormat": {"type": "NUMBER", "pattern": _PERCENT}}
            )
            worksheet.columns_auto_resize(0, 1)
        except Exception:  # noqa: BLE001
            pass

        # サマリーを先頭シートに持ってくる
        try:
            self._open().reorder_worksheets(
                [worksheet] + [
                    w for w in self._open().worksheets() if w.title != SHEET_SUMMARY
                ]
            )
        except Exception:  # noqa: BLE001
            pass


# ── ヘルパー ────────────────────────────────────────────────
def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value else ""


def _col_letter(index: int) -> str:
    """1 → A, 27 → AA。"""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"
