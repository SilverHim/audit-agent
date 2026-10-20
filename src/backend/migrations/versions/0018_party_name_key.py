"""往来名称规范化函数 party_name_key(name, prefixes, suffixes) (需求 R132): 规则 SQL (GEN-05 R11, R12, 后续 GEN-03)
比较对方名称用. 步骤与 services/party_names.py 的 party_name_key 逐步相同 (说明见该模块): NFKD 分解并去掉附加符
(normalize, 库编码须为 UTF8), 只认 ASCII 字母与数字, 不随数据库区域设置变化 (转小写用 translate, 正则按 "C" 排序
规则); 没有可比的名称本身 (空串) 时返回 NULL. 函数不读表, 标 IMMUTABLE; name 为空返回空, 两个数组为空 (NULL 或 {})
时跳过对应步骤. 新库 (0001 按模型建表, 不含函数) 与已有数据的库都在这里建; CREATE OR REPLACE 可重复执行."""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = "abcdefghijklmnopqrstuvwxyz"
COMBINING = (
    r"[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]"  # 去掉的附加符, 同 party_names.COMBINING
)


def folded(expr: str) -> str:
    """第 1 步的 SQL: NFKD 分解, 去掉附加符, 再只转 ASCII 大写字母."""
    stripped = f"regexp_replace(normalize({expr}, NFKD) COLLATE \"C\", '{COMBINING}', '', 'g')"
    return f"translate({stripped}, '{UPPER}', '{LOWER}')"


FUNCTION = f"""
CREATE OR REPLACE FUNCTION party_name_key(name text, prefixes text[], suffixes text[])
RETURNS text LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
  t text;
  k text;
  p text;
  s text;
  best text := '';
BEGIN
  IF name IS NULL THEN
    RETURN NULL;
  END IF;
  t := btrim({folded("name")});
  FOREACH p IN ARRAY coalesce(prefixes, '{{}}'::text[]) LOOP
    p := {folded("p")};
    IF p <> '' AND left(t, length(p)) = p AND length(p) > length(best)
       AND (right(p, 1) COLLATE "C" !~ '[a-z0-9]' OR substr(t, length(p) + 1, 1) COLLATE "C" !~ '[a-z0-9]') THEN
      best := p;
    END IF;
  END LOOP;
  k := btrim(regexp_replace(substr(t, length(best) + 1) COLLATE "C", '[^a-z0-9]+', ' ', 'g'));
  best := '';
  FOREACH p IN ARRAY coalesce(suffixes, '{{}}'::text[]) LOOP
    s := btrim(regexp_replace({folded("p")} COLLATE "C", '[^a-z0-9]+', ' ', 'g'));
    IF s <> '' AND (k = s OR right(k, length(s) + 1) = ' ' || s) AND length(s) > length(best) THEN
      best := s;
    END IF;
  END LOOP;
  IF best <> '' THEN
    k := left(k, greatest(length(k) - length(best) - 1, 0));
  END IF;
  RETURN nullif(k, '');
END
$$
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute("GRANT EXECUTE ON FUNCTION party_name_key(text, text[], text[]) TO audit_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS party_name_key(text, text[], text[])")
