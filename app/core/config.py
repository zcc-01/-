"""全局配置（公共基础层）。

设计取舍：
- 不引入 pydantic-settings 等额外依赖，用一个不可变的 dataclass + 极简 .env 解析，
  评审者在 Codespaces 里 `pip install -r requirements.txt` 后即可跑，减少装包失败点。
- 所有可变项都从环境变量读取，密钥只允许走环境变量 / .env，不落代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载：只支持 KEY=VALUE 与 # 注释，已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


_load_dotenv(REPO_ROOT / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """运行期配置。字段名与 .env.example 一一对应。"""

    app_name: str = "农产品溯源与产地直供电商平台"
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "dev"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{(REPO_ROOT / 'data' / 'app.db').as_posix()}"
        )
    )

    # --- 鉴权（UC-11）---
    jwt_secret: str = field(
        default_factory=lambda: os.getenv("JWT_SECRET", "dev-only-change-me-in-production")
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = field(default_factory=lambda: _env_int("JWT_EXPIRE_MINUTES", 120))
    password_min_length: int = field(default_factory=lambda: _env_int("PASSWORD_MIN_LENGTH", 8))
    # 登录失败锁定：连续失败 N 次锁定 M 分钟（报告 3.6 安全性要求）
    login_max_fail: int = field(default_factory=lambda: _env_int("LOGIN_MAX_FAIL", 5))
    login_lock_minutes: int = field(default_factory=lambda: _env_int("LOGIN_LOCK_MINUTES", 15))

    # --- 验证码登录（UC-11 泛化子用例）---
    sms_code_ttl_seconds: int = field(default_factory=lambda: _env_int("SMS_CODE_TTL_SECONDS", 300))
    sms_code_length: int = 6

    # --- 产地认证（UC-12）---
    cert_valid_months: int = field(default_factory=lambda: _env_int("CERT_VALID_MONTHS", 12))
    cert_remind_days: int = field(default_factory=lambda: _env_int("CERT_REMIND_DAYS", 30))
    cert_review_sla_days: int = field(default_factory=lambda: _env_int("CERT_REVIEW_SLA_DAYS", 3))
    # 检测报告有效期（月），超过视为过期材料，审核不通过（BR：检测报告过期）
    test_report_valid_months: int = field(default_factory=lambda: _env_int("TEST_REPORT_VALID_MONTHS", 12))

    # --- 预警（UC-14）---
    alert_scan_minutes: int = field(default_factory=lambda: _env_int("ALERT_SCAN_MINUTES", 30))
    alert_enable_scheduler: bool = field(default_factory=lambda: _env_bool("ALERT_ENABLE_SCHEDULER", True))
    # 规则一：采收后 N 天仍无物流环节
    alert_no_logistics_days: int = field(default_factory=lambda: _env_int("ALERT_NO_LOGISTICS_DAYS", 7))
    # 规则二：24 小时内跨省阈值
    alert_cross_province_threshold: int = field(default_factory=lambda: _env_int("ALERT_CROSS_PROVINCE", 3))
    alert_cross_province_hours: int = field(default_factory=lambda: _env_int("ALERT_CROSS_HOURS", 24))

    # --- 开发便利开关：仅在 dev 环境开放 /api/auth/dev-login 与 /api/ops/alerts/simulate ---
    dev_mode: bool = field(default_factory=lambda: _env_bool("DEV_MODE", True))

    # --- 分页 ---
    default_page_size: int = 20
    max_page_size: int = 100

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
