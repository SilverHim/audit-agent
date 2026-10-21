"""试算表加已验证更正分录的数据库函数 adjusted_trial_balance(p_eid, p_entry_nos) (需求 R174): 规则 SQL 取更正后余额
用, 口径同 services/adjusted_tb.adjusted_balances: 项目本期试算表的余额加凭证号在 p_entry_nos 中的总账分录行 (借方
减贷方), 按科目合计, 试算表没有的科目随分录行新出现; 返回 (account_code, balance_cents), 行序不定, 要顺序的调用方
自己 ORDER BY (函数会被内联进调用方的查询, 调用方 JOIN 与汇总后行序本来就不保留; emit: exception 的规则自带 ORDER BY).
p_entry_nos 为空 (NULL 或 {}, = ANY(NULL) 不为真, 不取分录行) 时即本期试算表. 凭证号由派生引用
verified_adjustment_entry_nos 给出 (已验证更正分录). SECURITY INVOKER (缺省, 写明): 以调用者的角色读表, 行级安全
照常生效, 跨项目读不到. 函数只读, 标 STABLE. CREATE OR REPLACE 可重复执行, 新库 (0001 按模型建表, 不含函数) 与
已有数据的库都在这里建; 降级删除函数."""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION adjusted_trial_balance(p_eid text, p_entry_nos text[])
RETURNS TABLE (account_code text, balance_cents bigint)
LANGUAGE sql STABLE SECURITY INVOKER AS $$
  SELECT x.account_code, SUM(x.cents)::bigint AS balance_cents
  FROM (
    SELECT t.account_code, t.balance_cents AS cents FROM trial_balance t
     WHERE t.engagement_id = p_eid AND t.period = 'current'
    UNION ALL
    SELECT j.account_code, j.debit_cents - j.credit_cents FROM journal_entries j
     WHERE j.engagement_id = p_eid AND j.entry_no = ANY(p_entry_nos)
  ) x
  GROUP BY x.account_code
$$
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute("GRANT EXECUTE ON FUNCTION adjusted_trial_balance(text, text[]) TO audit_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS adjusted_trial_balance(text, text[])")
