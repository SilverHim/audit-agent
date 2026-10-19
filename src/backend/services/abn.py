"""ABN 的格式与校验位 (ABR 公布的算法, https://abr.business.gov.au/Help/AbnFormat, Version 9.9.7, 2026-10-19 核对).

ABN 为 11 位数字: 9 位编号前加 2 位校验位, 校验位按模 89 算出. 校验: 第一位 (最左) 减 1 得到新的 11 位数, 各位依次
乘权重 10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 求和除以 89, 余数为 0 即有效. 官方例子: 51 824 753 556 → 减 1 后
41 824 753 556, 加权和 534, 534 = 89 × 6.
"""

import re

WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)  # ABR 公布的各位权重, 从左到右


def normalize_abn(abn: str) -> str:
    """去掉空白 (ABN 常写成 51 824 753 556)."""
    return "".join(abn.split())


def abn_problem(abn: str) -> str | None:
    """ABN (可含空格) 不合格时的说明, 合格为 None: 去空白后须为 11 位 ASCII 数字, 且校验位按 ABR 的算法正确."""
    digits = normalize_abn(abn)
    if not re.fullmatch(r"[0-9]{11}", digits):
        return f"ABN 须为 11 位数字: {abn!r}"
    values = [int(ch) for ch in digits]
    values[0] -= 1
    if sum(v * w for v, w in zip(values, WEIGHTS, strict=True)) % 89 != 0:
        return f"ABN {digits} 的校验位不对 (按 ABR 公布的模 89 算法)"
    return None
