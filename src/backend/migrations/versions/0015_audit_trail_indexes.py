"""留痕表按项目读取的索引 (需求 R298): (engagement_id, created_at, id), (engagement_id, kind, created_at, id) 与
(engagement_id, idempotency_key) 的部分索引 (只含 idempotency_key 不为空的行). 定义取自 persistence/models.py 的
TRAIL_BY_TIME, TRAIL_BY_KIND, TRAIL_BY_KEY. 已有数据的库上照常建 (留痕表只增不改, 建索引期间暂停写入); 新库 0001 按
当前模型 create_all 已建, 这里跳过. 各语句都可重复执行."""

from alembic import op

from backend.persistence.models import TRAIL_BY_KEY, TRAIL_BY_KIND, TRAIL_BY_TIME

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

INDEXES = (TRAIL_BY_TIME, TRAIL_BY_KIND, TRAIL_BY_KEY)


def upgrade() -> None:
    for index in INDEXES:
        index.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for index in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {index.name}")
