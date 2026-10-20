# Xero 演示的大模型 fixture 正本

- 每个文件 `<程序>/<对象>.json`, 内容为该程序 output_model 的 JSON; fixture_key 同接口文档第 4 节 MockLLMClient.
- DOC-CLASSIFY (上传分类) 按上传的文件名主干取: 补造目录中要上传的 22 份 PDF 各一份, 类型同 manifest.documents; 不登记的 fs_v1.pdf 另有一份, 为 financial_statements; default.json 判断不了 (document_type 为空).
- DOC-STATEMENTS (待审财报抽取, 需求 R217) 按文件名主干取: fs_v1.json 即补造目录 financial_statements/fs_v1.json 去掉 id, version 与 document_id (由系统补上); 改了补造的报表要同步改它 (守卫: tests/test_documents_upload.py).
- document 指针直接用 data/xero_demo/20261017-q1/manifest.json 的文件 ID (DOC- 加 sha256 前 12 位), 不用占位 ID.
- 加或改 fixture 的需求须重跑 `uv run python data/generate_xero_demo.py`, 把运行副本 data/xero_demo/20261017-q1/llm/ 一起提交.
- 守卫: tests/test_knowledge.py (正本与运行副本相同), tests/test_xero_fixtures.py (指针, 程序与输出模型).
