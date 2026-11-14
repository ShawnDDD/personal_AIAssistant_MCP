"""全局配置。

所有配置项都可用 MCP_ 前缀的环境变量覆盖，例如 MCP_DATA_DIR。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/mcp_common/config.py -> parents[2] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 把 .env 里的自定义变量注入环境，供流程 YAML 的 {{ env.XXX }} 使用
load_dotenv(PROJECT_ROOT / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="MCP_",
        extra="ignore",
        case_sensitive=False,
    )

    data_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "data")
    backup_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "backups")

    # 每个文件最多保留的备份份数
    backup_keep: int = 20
    # Excel 写操作默认只演练不落盘
    dry_run_default: bool = True

    log_level: str = "INFO"
    log_file: Path | None = None

    # 网页自动化
    web_headless: bool = False
    web_timeout_ms: int = 30_000
    web_max_rows: int = 500

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def flows_dir(self) -> Path:
        return PROJECT_ROOT / "flows"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.backup_dir,
            self.data_dir / "exports",
            self.profiles_dir,
            self.runs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
