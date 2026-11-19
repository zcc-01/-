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

    # --- 鉴权 ---
    jwt_secret: str = field(
        default_factory=lambda: os.getenv("JWT_SECRET", "dev-only-change-me-in-production")
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = field(default_factory=lambda: _env_int("JWT_EXPIRE_MINUTES", 120))

    # --- 溯源链 ---
    # 创世盐值：首条记录的 prev_hash = sha256(f"{GENESIS_SALT}|{batch_id}")
    # 固定常量而非 uuid4，保证同一批次的创世基准可重现（报告 5.2.1 的人工修改点）。
    genesis_salt: str = field(default_factory=lambda: os.getenv("GENESIS_SALT", "GENESIS"))
    chain_page_size: int = field(default_factory=lambda: _env_int("CHAIN_PAGE_SIZE", 500))
    chain_max_retry: int = field(default_factory=lambda: _env_int("CHAIN_MAX_RETRY", 5))

    # --- 赋码 ---
    max_code_batch: int = field(default_factory=lambda: _env_int("MAX_CODE_BATCH", 5000))
    code_prefix_box: str = "BOX"
    code_prefix_item: str = "ITM"

    # --- 开发便利开关：仅在 dev 环境开放 /api/auth/dev-login ---
    dev_mode: bool = field(default_factory=lambda: _env_bool("DEV_MODE", True))

    # --- 分页 ---
    default_page_size: int = 20
    max_page_size: int = 100

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
