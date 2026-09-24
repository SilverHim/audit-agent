"""配置项，键名见 开发接口文档.md 第 11 节。从环境变量与 .env 读取。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    DATABASE_URL: str = "postgresql://audit_app:audit_app@localhost:5432/audit"
    DATABASE_OWNER_URL: str = "postgresql://audit_owner:audit_owner@localhost:5432/audit"
    CHECKPOINT_BACKEND: Literal["postgres", "memory"] = "postgres"  # memory 只用于测试

    EVIDENCE_ROOT: Path = ROOT / "var" / "engagements"
    DATASET_ROOT: Path = ROOT / "data"
    KNOWLEDGE_ROOT: Path = ROOT / "src" / "backend" / "knowledge"

    LLM_MODE: Literal["mock", "live"] = "mock"
    LLM_MODEL: str = ""
    LLM_API_KEY: str = ""
    LLM_FIXTURES_ROOT: Path = ROOT / "tests" / "fixtures" / "llm"
    PARALLEL_CAP: int = 4

    JWT_SECRET: str = "change-me"
    JWT_EXPIRE_MINUTES: int = 480
    SEED_USER_EMAIL: str = "rca@example.com"
    SEED_USER_PASSWORD: str = "change-me"
    FRONTEND_ORIGIN: str = "http://localhost:5173"

    @property
    def sqlalchemy_url(self) -> str:
        """SQLModel / SQLAlchemy 使用 psycopg 驱动。"""
        return self.DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
