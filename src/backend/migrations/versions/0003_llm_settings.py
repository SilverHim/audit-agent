"""设置页的模型配置表 llm_settings: 系统级, 不按项目隔离 (同 users, 不开行级安全)."""

from alembic import op

from backend.persistence.models import llm_settings

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已建出此表; 旧库补建
    llm_settings.create(op.get_bind(), checkfirst=True)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON llm_settings TO audit_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_settings")
