"""往来名称规范化 (需求 R132; Xero 演示数据集设计第 5.1 节 GEN-03 C01, 第 5.3 节 R11, R12): 把分录说明, 单据号,
银行流水收款人或发票客户中的对方名称化成可比较的键. 本函数与迁移 0018 的 SQL 函数 party_name_key 逐步相同, 规则 SQL
用后者, Python 侧 (测试与 GEN-03 的上下文) 用本函数, 两侧对同一组名称结果相同 (tests/test_party_names.py 守住).

步骤:
1. 先做 Unicode 分解 (NFKD) 并去掉附加符 (COMBINING 所列的组合用字符), 带重音的拉丁字母留下字母本身, 同 R136 的
   models.node_name_part ("Café" 同 "Cafe", "José" 同 "Jose"); 再只把 ASCII 大写字母转小写 (不随数据库区域设置
   变化), 去首尾空格.
2. 去掉开头的一个前缀 (prefixes, 如 "payment to", "payment:"; 同样经第 1 步): 取匹配的最长一个; 前缀以字母或数字
   结尾时, 其后须是串尾或非字母数字 (整词, "payment to" 不吃掉 "Payment Tools" 的 "payment to").
3. 连续的非字母数字字符 (ASCII 字母与数字以外的一切字符: 标点, 空白, 非拉丁字母如中文, 及分解后仍不是 ASCII 的
   字母如 ß) 换成一个空格, 去首尾空格.
4. 去掉末尾的一个后缀 (suffixes, 如 "pty ltd", "pty"; 先经第 1, 3 步): 须是整词, 取匹配的最长一个.
5. 结果为空串 (名称全是非拉丁字符, 只剩前缀或只剩公司后缀, 没有可比的名称本身) 时返回 None, SQL 返回 NULL: 空键
   不与任何名称相等 (SQL 的 = 与 = ANY 遇 NULL 不成立).

name 为 None 时返回 None; prefixes, suffixes 为 None 或空时跳过对应步骤.
"""

import re
import string
import unicodedata
from collections.abc import Sequence

ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
# 分解后去掉的附加符: 组合用附加符号及其扩充与补充, 符号用组合附加符, 组合用半符号 (SQL 函数用同一组区间)
COMBINING = re.compile(r"[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")
SEPARATORS = re.compile(r"[^a-z0-9]+")
ALNUM = re.compile(r"[a-z0-9]")


def folded(text: str) -> str:
    """第 1 步: NFKD 分解, 去掉附加符, 再只转 ASCII 大写字母 (同 SQL 的 translate), 其他字符不动."""
    return COMBINING.sub("", unicodedata.normalize("NFKD", text)).translate(ASCII_LOWER)


def separated(text: str) -> str:
    """非字母数字换成一个空格, 去首尾空格 (第 3 步)."""
    return SEPARATORS.sub(" ", text).strip(" ")


def party_name_key(
    name: str | None, prefixes: Sequence[str] | None = (), suffixes: Sequence[str] | None = ()
) -> str | None:
    """名称的规范化键 (步骤见模块说明); 没有可比的名称本身时为 None."""
    if name is None:
        return None
    text = folded(name).strip(" ")
    prefix = ""
    for raw in prefixes or ():
        p = folded(raw)
        whole_word = not ALNUM.match(p[-1:]) or not ALNUM.match(text[len(p) : len(p) + 1])
        if p and text.startswith(p) and len(p) > len(prefix) and whole_word:
            prefix = p
    key = separated(text[len(prefix) :])
    suffix = ""
    for raw in suffixes or ():
        s = separated(folded(raw))
        if s and (key == s or key.endswith(" " + s)) and len(s) > len(suffix):
            suffix = s
    if suffix:
        key = key[: max(len(key) - len(suffix) - 1, 0)]
    return key or None
