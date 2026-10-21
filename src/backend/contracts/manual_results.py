"""人工程序的结果模型登记 (需求 R92, 开发接口文档第 8.1 节): 程序 YAML 的 manual.result_model 写的名字 → 校验
manual_result.details 的模型. 程序库加载时核对 YAML 写的名字都已登记; 提交 manual_result 时按任务请求里的名字取模型
校验 details (decisions.check_manual_result). 新的人工程序在 RESULT_MODELS 登记一行; 测试可临时登记
(monkeypatch.setitem)."""

from pydantic import BaseModel

from backend.contracts.models import ManagementResponse, Model


class ManagementResponses(Model):
    """FIN-08 的登记表 (需求 R161): 与管理层沟通的错报每条一行 (沟通的时间与对象, 回复的时间与原文, 是否同意更正),
    须恰好覆盖派发材料中的错报 (A8 的 check 核对)."""

    responses: list[ManagementResponse]


RESULT_MODELS: dict[str, type[BaseModel]] = {  # 结果模型名 → 模型 (宜继承 contracts.models.Model, 多出的字段报错)
    "ManagementResponses": ManagementResponses,
}


def result_model(name: str) -> type[BaseModel] | None:
    """按名取结果模型, 没登记为 None. 程序库加载时只核对当前模板写的名字; 派发之后模板改了名字或登记删了它, 旧任务
    请求里的名字就取不到 (提交时 decisions.check_manual_result 拒绝)."""
    return RESULT_MODELS.get(name)
