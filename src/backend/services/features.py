"""功能开关 (计划第 1 节决定 1): 新程序与新机制按功能名启用. 功能关闭时程序库, 固定集, 装配, 主图与 Agent 行为与
现状一致; 程序 YAML 的 feature 字段为空即基线, 总是启用.

通用化改动与人工处理队列机制不设开关: 前者要求 Numbat 结果不变, 后者在没有启用的人工程序时不起作用.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureSpec:
    """一个功能: 说明, 以及须同时启用的其他功能."""

    description: str
    requires: frozenset[str] = frozenset()


# 功能名 → 说明与依赖. requires 为建议值, 各组的首条需求可改
FEATURES: dict[str, FeatureSpec] = {
    "critic": FeatureSpec("A7 独立复核 (GEN-09); 基线功能, 缺省开启"),
    "collab": FeatureSpec(
        "智能体协作增量: A7 触发规则补齐, 争议中状态, 仲裁补全, 判断呈递, 停留时长", frozenset({"critic"})
    ),
    "journals": FeatureSpec("分录测试: GEN-05, GEN-02"),
    "related_parties": FeatureSpec("关联方: GEN-08, GEN-03, GEN-01"),
    "going_concern": FeatureSpec("持续经营: GEN-07, GEN-06, GEN-04, FIN-02 与 gate_fin 两步, 报告持续经营段"),
    "misstatement": FeatureSpec("错报闭环: FIN-03, FIN-08, FIN-09, 等待更正, FIN-04 按状态累计, P3 骨架节点"),
    "autonomy": FeatureSpec("自主度: 动态降级"),
    "acceptance": FeatureSpec("承接: ACC-08 与其闸门"),
    "planning": FeatureSpec("计划: PLN-15, PLN-19 与其闸门", frozenset({"misstatement"})),
    "presign": FeatureSpec(
        "签字前: FIN-12, FIN-15, FIN-07, FIN-18, FIN-10, RPT-08, RPT-07, 签字前清单", frozenset({"misstatement"})
    ),
}
BASELINE: frozenset[str] = frozenset({"critic"})  # 缺省功能集; Numbat 回归测试显式用它 (tests/fakes.NUMBAT_FEATURES)
COLLAB = "collab"  # 智能体协作增量的功能名 (A7 触发规则, 争议中状态与单列, 仲裁补全等按它开关)
MISSTATEMENT = "misstatement"  # 错报闭环的功能名 (需求 R156 起: 新错报为 identified 并走生命周期; 关闭时为 uncorrected)
PLANNING = "planning"  # 计划的功能名: 装配后起草总体审计计划, 停在 PLN-15 闸门, 计划批准才进 P2 (需求 R205)


def features_text(features: Iterable[str]) -> str:
    """功能集冻结进 version_snapshot["features"] 的写法: 功能名排序后以逗号连接 (基线为 "critic", 全关为空串)."""
    return ",".join(sorted(features))


LEGACY_FEATURES = features_text(BASELINE)  # 冻结功能集之前建的项目 (version_snapshot 没有 features 键) 按基线处理


def frozen_features(version_snapshot: Mapping[str, str], current: Iterable[str]) -> frozenset[str]:
    """项目的功能集: 已启动 (有版本快照) 取冻结的 (没有 features 键的旧项目按基线), 还没启动取当前进程的 current."""
    if not version_snapshot:
        return frozenset(current)
    return parse_features(version_snapshot.get("features", LEGACY_FEATURES))


def parse_features(text: str) -> frozenset[str]:
    """逗号分隔的功能名 (如 "critic,journals"), 各项去掉首尾空白, 空项忽略; 空串为空集 (全部关闭, 含 critic).
    只拆分, 不校验 (见 check_features)."""
    return frozenset(name.strip() for name in text.split(",") if name.strip())


def check_features(features: Iterable[str]) -> frozenset[str]:
    """校验功能集并返回 frozenset: 有未登记的名字, 或某功能 requires 的功能不在集内时抛 ValueError, 消息列出名字."""
    chosen = frozenset(features)
    unknown = sorted(chosen - FEATURES.keys())
    if unknown:
        raise ValueError(f"未登记的功能: {', '.join(unknown)} (已登记: {', '.join(FEATURES)})")
    unmet = [
        f"{name} 需要 {', '.join(sorted(FEATURES[name].requires - chosen))}"
        for name in sorted(chosen)
        if FEATURES[name].requires - chosen
    ]
    if unmet:
        raise ValueError(f"功能依赖不满足: {'; '.join(unmet)}")
    return chosen
