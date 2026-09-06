"""schema 引导的结构化抽取（#661，设计报告 §10 ②层）。

schemas.py 是抽取 schema 注册表（内置通用骨架 skeleton@1，学科包后续经
``register_schema`` 挂载）；runtime.py 是抽取运行时（取正文 → LLM 抽取 →
归一化 → UPSERT 落 paper_extractions）。
"""
