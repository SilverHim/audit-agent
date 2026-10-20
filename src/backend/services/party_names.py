"""往来名称规范化 (需求 R132; Xero 演示数据集设计第 5.1 节 GEN-03 C01, 第 5.3 节 R11, R12): 把分录说明, 单据号,
银行流水收款人或发票客户中的对方名称化成可比较的键. 本函数与迁移 0018 的 SQL 函数 party_name_key 逐步相同, 规则 SQL
用后者, Python 侧 (测试与 GEN-03 的上下文) 用本函数, 两侧对同一组名称结果相同 (tests/test_party_names.py 守住).

步骤:
1. 只把 ASCII 大写字母转小写 (不随数据库区域设置变化), 去首尾空格.
2. 去掉开头的一个前缀 (prefixes, 如 "payment to", "payment:"; 同样只转 ASCII 小写): 取匹配的最长一个; 前缀以字母
   或数字结尾时, 其后须是串尾或非字母数字 (整词, "payment to" 不吃掉 "Payment Tools" 的 "payment to").
3. 连续的非字母数字字符 (ASCII 字母与数字以外的一切字符, 含中文与带重音的字母) 换成一个空格, 去首尾空格.
4. 去掉末尾的一个后缀 (suffixes, 如 "pty ltd", "pty"; 先按第 1, 3 步规范化): 须是整词且前面还有内容, 取匹配的最长
   一个. 名称只剩后缀时不去 ("Limited" 仍为 "limited").

name 为 None 时返回 None; prefixes, suffixes 为 None 或空时跳过对应步骤.
"""

import re
import string
from collections.abc import Sequence

ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
SEPARATORS = re.compile(r"[^a-z0-9]+")
ALNUM = re.compile(r"[a-z0-9]")


def ascii_lower(text: str) -> str:
    """只转 ASCII 大写字母 (同 SQL 的 translate), 其他字符不动."""
    return text.translate(ASCII_LOWER)


def separated(text: str) -> str:
    """非字母数字换成一个空格, 去首尾空格 (第 3 步)."""
    return SEPARATORS.sub(" ", text).strip(" ")


def party_name_key(
    name: str | None, prefixes: Sequence[str] | None = (), suffixes: Sequence[str] | None = ()
) -> str | None:
    """名称的规范化键 (步骤见模块说明)."""
    if name is None:
        return None
    text = ascii_lower(name).strip(" ")
    prefix = ""
    for raw in prefixes or ():
        p = ascii_lower(raw)
        whole_word = not ALNUM.match(p[-1:]) or not ALNUM.match(text[len(p) : len(p) + 1])
        if p and text.startswith(p) and len(p) > len(prefix) and whole_word:
            prefix = p
    key = separated(text[len(prefix) :])
    suffix = ""
    for raw in suffixes or ():
        s = separated(ascii_lower(raw))
        if s and key.endswith(" " + s) and len(s) > len(suffix):
            suffix = s
    return key[: len(key) - len(suffix) - 1] if suffix else key
