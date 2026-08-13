"""設定の読み込み。環境変数と .env を単一の Config にまとめる。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv は任意。無ければ環境変数のみを見る。
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 依存が入っていない環境向け
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """アプリ全体の設定。"""

    data_dir: Path = Path("./data")
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5"
    effort: str = "medium"

    google_service_account_json: str | None = None
    google_spreadsheet_key: str | None = None
    google_spreadsheet_name: str = "メルカリ売上管理"

    allow_http_research: bool = False
    research_endpoint: str | None = None
    research_interval_sec: float = 3.0

    target_margin: float = 0.30
    min_margin: float = 0.12

    #: 手数料・送料テーブルの置き場所（未指定ならパッケージ同梱の data/fees.json）
    fees_path: Path | None = field(default=None)

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> "Config":
        """.env と環境変数から Config を組み立てる。

        env_file を明示しない場合はカレントディレクトリの .env を探す。
        既に設定済みの環境変数を .env が上書きすることはない。
        """
        if env_file is not None:
            load_dotenv(env_file, override=False)
        else:
            load_dotenv(override=False)

        return cls(
            data_dir=Path(os.getenv("MERCARI_DATA_DIR", "./data")).expanduser(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            model=os.getenv("MERCARI_MODEL", "claude-opus-5"),
            effort=os.getenv("MERCARI_EFFORT", "medium"),
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
            google_spreadsheet_key=os.getenv("GOOGLE_SPREADSHEET_KEY") or None,
            google_spreadsheet_name=os.getenv("GOOGLE_SPREADSHEET_NAME", "メルカリ売上管理"),
            allow_http_research=_env_bool("MERCARI_ALLOW_HTTP_RESEARCH", False),
            research_endpoint=os.getenv("MERCARI_RESEARCH_ENDPOINT") or None,
            research_interval_sec=_env_float("MERCARI_RESEARCH_INTERVAL", 3.0),
            target_margin=_env_float("MERCARI_TARGET_MARGIN", 0.30),
            min_margin=_env_float("MERCARI_MIN_MARGIN", 0.12),
        )

    # ── 便利プロパティ ────────────────────────────────────────
    @property
    def products_path(self) -> Path:
        return self.data_dir / "products.json"

    @property
    def sales_path(self) -> Path:
        return self.data_dir / "sales.json"

    @property
    def purchases_path(self) -> Path:
        return self.data_dir / "purchases.json"

    @property
    def suppliers_path(self) -> Path:
        return self.data_dir / "suppliers.json"

    @property
    def comps_dir(self) -> Path:
        return self.data_dir / "comps"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.comps_dir, self.output_dir):
            path.mkdir(parents=True, exist_ok=True)
