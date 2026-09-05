"""全局配置：从环境变量与 .env 读取，不打印任何密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _load_dotenv() -> None:
    """极简 .env 加载，避免额外依赖。已存在的环境变量优先。"""
    for candidate in (ROOT / ".env",):
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class LLMConfig:
    """模型接入配置：可替换供应商，统一走 OpenAI 兼容协议。"""

    enabled: bool = field(default=True)
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    timeout: int = 120
    max_input_chars: int = 24000
    max_output_tokens: int = 2000
    # 独立模型批次的并发数。默认 2，在不改变输入、参数和校验的前提下缩短等待时间。
    parallel_calls: int = 2
    temperature: float = 0.1
    # 单次扫描预算（人民币）。未配置单价时按 0 计，仅累计 token 量。
    budget_cny: float = 3.0
    price_in_cny_per_1m: float = 0.0
    price_out_cny_per_1m: float = 0.0
    verify_enabled: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class Settings:
    app_name: str = "基本面排雷 Agent"
    host: str = "127.0.0.1"
    port: int = 8770
    debug: bool = False

    # 存储
    db_path: Path = DATA_DIR / "app.db"
    files_dir: Path = DATA_DIR / "files"
    reports_dir: Path = DATA_DIR / "reports"
    cache_dir: Path = DATA_DIR / "cache"

    # 网络
    http_timeout: int = 30
    download_timeout: int = 120
    http_retries: int = 3
    http_backoff: float = 1.5
    per_host_rps: float = 2.0
    max_file_bytes: int = 60 * 1024 * 1024
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    allow_private_address: bool = False

    # 扫描范围
    fiscal_years_back: int = 5
    announcement_months: int = 12
    max_announcements: int = 300
    max_pdf_pages: int = 120
    max_pdf_downloads: int = 25
    scan_timeout_seconds: int = 900

    # 功能开关
    enable_network: bool = True
    enable_pdf_parse: bool = True
    enable_llm: bool = True

    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def llm_ready(self) -> bool:
        return bool(self.enable_network and self.enable_llm and self.llm.enabled and self.llm.configured)


def load_settings() -> Settings:
    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True)
    for sub in ("cache", "files", "reports"):
        target = DATA_DIR / sub
        if not target.exists():
            target.mkdir(parents=True)

    llm = LLMConfig(
        enabled=_b("LLM_ENABLED", True),
        api_key=os.environ.get("LLM_API_KEY", "").strip(),
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").strip(),
        model=os.environ.get("LLM_MODEL", "deepseek-chat").strip(),
        timeout=_i("LLM_TIMEOUT", 120),
        max_input_chars=_i("LLM_MAX_INPUT_CHARS", 24000),
        max_output_tokens=_i("LLM_MAX_OUTPUT_TOKENS", 2000),
        parallel_calls=max(1, _i("LLM_PARALLEL_CALLS", 2)),
        temperature=_f("LLM_TEMPERATURE", 0.1),
        budget_cny=_f("LLM_BUDGET_CNY", 3.0),
        price_in_cny_per_1m=_f("LLM_PRICE_IN_CNY_PER_1M", 0.0),
        price_out_cny_per_1m=_f("LLM_PRICE_OUT_CNY_PER_1M", 0.0),
        verify_enabled=_b("LLM_VERIFY_ENABLED", True),
    )

    return Settings(
        app_name=os.environ.get("APP_NAME", "基本面排雷 Agent"),
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=_i("APP_PORT", 8770),
        debug=_b("APP_DEBUG", False),
        db_path=Path(os.environ.get("DB_PATH", str(DATA_DIR / "app.db"))),
        http_timeout=_i("HTTP_TIMEOUT", 30),
        download_timeout=_i("DOWNLOAD_TIMEOUT", 120),
        http_retries=_i("HTTP_RETRIES", 3),
        http_backoff=_f("HTTP_BACKOFF", 1.5),
        per_host_rps=_f("PER_HOST_RPS", 2.0),
        max_file_bytes=_i("MAX_FILE_BYTES", 60 * 1024 * 1024),
        allow_private_address=_b("ALLOW_PRIVATE_ADDRESS", False),
        fiscal_years_back=_i("FISCAL_YEARS_BACK", 5),
        announcement_months=_i("ANNOUNCEMENT_MONTHS", 12),
        max_announcements=_i("MAX_ANNOUNCEMENTS", 300),
        max_pdf_pages=_i("MAX_PDF_PAGES", 120),
        max_pdf_downloads=_i("MAX_PDF_DOWNLOADS", 25),
        scan_timeout_seconds=_i("SCAN_TIMEOUT_SECONDS", 900),
        enable_network=_b("ENABLE_NETWORK", True),
        enable_pdf_parse=_b("ENABLE_PDF_PARSE", True),
        enable_llm=_b("ENABLE_LLM", True),
        llm=llm,
    )


settings = load_settings()
