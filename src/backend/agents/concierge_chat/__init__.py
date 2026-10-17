"""A10 审计助理的对话入口: 多轮对话, 带历史上下文, 调用只读工具回答进度与来源 (规格书 5.2 节, UC-08).

tools 只读工具, citations 引用匹配, loop 对话图 (LangGraph), memory 历史与压缩, rule_model mock 模式的规则模型,
service 对外的 ConciergeChat. 报告起草 (RPT-03) 仍在 agents/concierge.py.
"""
