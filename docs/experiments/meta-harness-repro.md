# 复现报告：Meta-Harness 检索增强数学推理（arXiv 2603.28052 §4.2 / Table 6）

> 用途：作为「从研究方案自动构建/分析实验」能力的端到端测试样例，同时驱动实验模块的增强
> （见 `backend/app/agents/voyage/actions_experiment.py` 的对照实验支持）。

## 1. 目标论文与要复现的实验

**Meta-Harness: End-to-End Optimization of Model Harnesses**（Lee, Nair, Zhang, Lee, Khattab, Finn，
Stanford 等，2026）搜索 LLM 的「harness 代码」（决定给固定模型存/取/呈现什么信息的代码）。
三个主实验之一是**检索增强数学推理**（§4.2 / Table 6）：给固定模型加「从大规模解题语料检索相似
题解」的能力，在 200 道 IMO 级题上比 pass@1。论文核心发现——**朴素检索（BM25/稠密）在数学推理上
并不可靠、常常回退**，只有被搜索发现的 harness 才稳定提升（+4.7），这正是「需要 harness 搜索」的
动机。

## 2. 抽取出的研究方案（Research Proposal）

作为实验模块的输入，从论文抽取一份结构化研究方案（idea 2.0 `depth=proposal` 的 goal + smoke_plan）：

- **任务**：检索增强的奥数题求解评测（training-free，固定模型经 API）
- **研究问题**：从解题语料检索相似题解注入上下文，能否提升固定 LLM 在难数学题上的准确率？
- **对照设计**：`no_retrieval`(baseline) vs `bm25` vs `dense`(BGE-M3) — 复现 Table 6 的核心对照
- **评测协议**：pass@1（答案判定），MATH-500 level-5 硬子集，多个 held-out API 模型
- **数据**：测试集 `HuggingFaceH4/MATH-500`（level-5）；检索语料 `AI-MO/NuminaMath-CoT`（竞赛题解）

## 3. 复现设置（受实验室资源约束的忠实缩放）

- **固定模型**（held-out）：`dashscope/qwen-turbo`、`dashscope/qwen-plus`（经实验室 LiteLLM 代理）。
  论文的 held-out 模型（GPT-5.4、Gemini、GPT-OSS-20B）也多为 API/小模型 → API 评测是忠实的。
  实验室 8×GPU 当时显存被占满，改走 API + CPU（检索），与论文设定一致且绕开 GPU 紧张。
- **测试集**：MATH-500 level-5（100 题，有可判定答案）。相比论文的 200 道 IMO 级题更易，
  但模型得分在 67%–77%（非 floor/ceiling），保留了检索起作用的头 room（忠实于「难但非 floor」）。
- **检索语料**：NuminaMath-CoT 竞赛题解 6000 条（对测试集去污染）。
- **检索**：BM25（rank_bm25，CPU）；稠密用 BGE-M3 embedding（经同一 LiteLLM 代理）+ 余弦 top-3。
- **指标**：pass@1 accuracy（`math-verify` 判定），k=3，每题 1 次采样；另记上下文长度。

## 4. 结果

| 模型 | no_retrieval | BM25 | dense |
|------|:---:|:---:|:---:|
| qwen-turbo | 67.0% | 63.0% (−4.0) | 64.0% (−3.0) |
| qwen-plus  | 77.0% | 76.0% (−1.0) | 76.0% (−1.0) |
| **平均**   | **72.0%** | **69.5% (−2.5)** | **70.0% (−2.0)** |

（每格 100 题、0 个 API 失败；BM25/dense 上下文约 5.4k–6.4k 字符 vs 无检索 ~430。）

## 5. 分析与结论

- **朴素检索回退**：BM25 与稠密检索相对无检索 baseline 均**下降**（平均 −2.5 / −2.0），
  在较弱的 qwen-turbo 上回退更大（−4.0 / −3.0），在较强的 qwen-plus 上接近中性（−1.0）。
- 这**忠实复现了 Meta-Harness §4.2 的核心负面现象**：朴素地把检索到的题解塞进上下文并不能
  可靠提升数学推理，反而常常回退（论文对多个模型也观察到稠密检索/随机 few-shot 回退）。
  这正是论文主张「需要搜索/发现 harness」的实证依据——本复现覆盖了其对照结构与关键结论，
  而未复现「被发现 harness 的 +4.7」（那需要论文的外层搜索循环，超出本测试样例范围）。
- 上下文膨胀：检索使上下文增大 ~13–15×，却未换来准确率——与论文「4× 更少 token 却更好」的
  对照互为印证（朴素检索是低效的）。

## 6. 这个测试样例对平台的意义（能力验证）

用这个复现驱动并验证了实验模块「从研究方案自动构建/分析对照实验」的能力：

1. **研究方案 → 实验计划**：把 proposal 的结构化 goal/smoke_plan 注入 `experiment.plan`，
   真实 `claude-opus-4-8` 产出了带 `conditions`(no_retrieval/bm25/dense) + `eval_protocol` +
   `datasets`(MATH-500 + NuminaMath) 的忠实计划。
2. **实验计划 → 实验代码**：放开真实数据集下载 + 对照约束后，`claude-opus-4-8` 生成了可运行的
   harness（读 llm_config.json 不硬编码 key、`load_dataset` 下真实数据、三条件同协议评测、
   逐条件 `POLARIS_METRIC`、`run.sh --smoke`）。
3. **对照分析**：`_conditions_delta` 确定性算出 baseline vs treatment 的 delta 表，喂给分析与
   报告，把「假设是否成立」锚定在对照结果而非单点指标。

参考 harness 代码见 `docs/experiments/meta-harness-harness/`（本次手工验证 + 作为模块代码生成的
对照参照）。
