"""配置项, 键名见 开发接口文档.md 第 11 节. 从环境变量与 .env 读取."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """应用配置: 从环境变量与 .env 读取, 键名见开发接口文档第 11 节."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")  # 读项目根目录的 .env, 忽略未定义的键

    DATABASE_URL: str = "postgresql://audit_app:audit_app@localhost:5432/audit"  # 运行时连接, 受行级安全约束
    DATABASE_OWNER_URL: str = "postgresql://audit_owner:audit_owner@localhost:5432/audit"  # 属主角色, 只用于迁移
    CHECKPOINT_BACKEND: Literal["postgres", "memory"] = "postgres"  # memory 只用于测试

    EVIDENCE_ROOT: Path = ROOT / "var" / "engagements"  # 证据原件根目录
    DATASET_ROOT: Path = ROOT / "data"  # 合成数据集目录
    KNOWLEDGE_ROOT: Path = ROOT / "src" / "backend" / "knowledge"  # 知识文件目录: 程序, 准则, prompt

    LLM_MODE: Literal["mock", "live"] = "mock"  # mock 用固定输出, live 调真实模型
    LLM_MODEL: str = ""  # live 模式的模型名
    LLM_API_KEY: str = ""  # live 模式的密钥, 只放 .env
    LLM_FIXTURES_ROOT: Path = ROOT / "tests" / "fixtures" / "llm"  # mock 模式的固定输出目录
    PARALLEL_CAP: int = 4  # fieldwork 实例并行上限

    XERO_CLIENT_ID: str = ""  # Xero 应用的 Client ID (不是密钥), 为空时不能接 Xero
    XERO_CLIENT_SECRET: str = ""  # Web app 类型的 client secret, 只放 .env; 为空时按 PKCE 授权
    XERO_REDIRECT_URI: str = "http://localhost:8765/callback"  # 授权回调地址, 须与 Xero 应用登记的一致
    XERO_TOKEN_PATH: Path = ROOT / "var" / "xero" / "token.json"  # 令牌文件, 含刷新令牌, 不入库
    XERO_TENANT_ID: str = ""  # 要拉取的组织, 为空时取授权连接里唯一的组织
    XERO_PAID_ENDPOINTS: Literal["live", "mock", "off"] = "off"  # 付费接口: 真实, 网页导出模拟, 不取 (退回单据反推)

    JWT_SECRET: str = "change-me"  # 登录令牌的签名密钥
    JWT_EXPIRE_MINUTES: int = 480  # 登录令牌有效期, 单位分钟
    SEED_USER_EMAIL: str = "rca@example.com"  # seed 脚本建的审计师账号
    SEED_USER_PASSWORD: str = "change-me"  # seed 账号的密码
    FRONTEND_ORIGIN: str = "http://localhost:5173"  # 允许跨域访问的前端地址

    @property
    def sqlalchemy_url(self) -> str:
        """SQLModel / SQLAlchemy 使用 psycopg 驱动."""
        return self.DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
