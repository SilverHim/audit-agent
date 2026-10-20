"""人工程序的结果模型登记 (需求 R92, 开发接口文档第 8.1 节): 程序 YAML 的 manual.result_model 写的名字 → 校验
manual_result.details 的模型. 程序库加载时核对 YAML 写的名字都已登记; 提交 manual_result 时按任务请求里的名字取模型
校验 details (decisions.check_manual_result). 新的人工程序在 RESULT_MODELS 登记一行; 测试可临时登记
(monkeypatch.setitem)."""

from pydantic import BaseModel

RESULT_MODELS: dict[str, type[BaseModel]] = {}  # 结果模型名 → 模型 (宜继承 contracts.models.Model, 多出的字段报错)


def result_model(name: str) -> type[BaseModel]:
    """按名取结果模型. 没登记抛 KeyError (程序库加载时已核对 YAML 写的名字都登记了)."""
    return RESULT_MODELS[name]
