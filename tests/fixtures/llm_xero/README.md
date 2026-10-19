# Xero 演示的大模型 fixture 正本

- 每个文件 `<程序>/<对象>.json`, 内容为该程序 output_model 的 JSON; fixture_key 同接口文档第 4 节 MockLLMClient.
- document 指针直接用 data/xero_demo/20261017-q1/manifest.json 的文件 ID (DOC- 加 sha256 前 12 位), 不用占位 ID.
- 加或改 fixture 的需求须重跑 `uv run python data/generate_xero_demo.py`, 把运行副本 data/xero_demo/20261017-q1/llm/ 一起提交.
- 守卫: tests/test_knowledge.py (正本与运行副本相同), tests/test_xero_fixtures.py (指针, 程序与输出模型).
