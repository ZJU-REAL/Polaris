/* ============================================================
   Auto-Research Studio — sample data
   Grounded in the real literature cited in the plan.
   Attached to window.DATA
   ============================================================ */
(function () {
  // ---- Directions registry (data/directions/registry.yaml) ----
  // Each direction is a self-contained LLM Wiki vault; only research-wiki writes here.
  const directions = [
    {
      slug: "llm-agentic-research", title: "LLM 自主科研智能体", titleEn: "LLM Agentic Research",
      status: "active", initialized: true, isDefault: true, bootstrap_months: 6, last_ingest: "2026-05-29",
      categories: ["cs.LG", "cs.AI", "cs.CL"],
      keywords: ["autonomous research agent", "research idea generation", "LLM scientist"],
      relevance_threshold: 0.6, target_venues: ["neurips2026", "iclr2027"],
      counts: { papers: 138, surveys: 11, concepts: 47, ideas: 9 },
    },
    {
      slug: "diffusion-rl", title: "扩散模型策略与强化学习", titleEn: "Diffusion Models for RL",
      status: "active", initialized: true, isDefault: false, bootstrap_months: 6, last_ingest: "2026-05-27",
      categories: ["cs.LG", "cs.RO"],
      keywords: ["diffusion policy", "diffusion planning", "RL fine-tuning of diffusion"],
      relevance_threshold: 0.62, target_venues: ["iclr2027", "corl2026"],
      counts: { papers: 64, surveys: 5, concepts: 23, ideas: 3 },
    },
    {
      slug: "neural-theorem-proving", title: "神经定理证明", titleEn: "Neural Theorem Proving",
      status: "active", initialized: false, isDefault: false, bootstrap_months: 6, last_ingest: null,
      categories: ["cs.LO", "cs.AI", "cs.LG"],
      keywords: ["neural theorem proving", "LLM + Lean", "premise selection", "autoformalization"],
      relevance_threshold: 0.6, target_venues: ["neurips2026"],
      counts: { papers: 0, surveys: 0, concepts: 0, ideas: 0 },
    },
  ];
  const direction = directions.find(d => d.isDefault);

  // ---- Papers (real arXiv works referenced in the plan) ----
  const papers = [
    {
      slug: "ai-scientist-v2", arxiv: "2504.08066", v: 1, kind: "paper",
      title: "The AI Scientist-v2: Workshop-Level Automated Scientific Discovery via Agentic Tree Search",
      authors: ["Yamada, Y.", "Lange, R. T.", "Lu, C.", "et al. (Sakana AI)"],
      published: "2026-04-10", ingested: "2026-05-29",
      tags: ["agentic-research", "tree-search", "end-to-end"],
      relevance: 0.91, status: "summarized", cites: ["2408.06292"],
      tldr_zh: "在 v1 基础上引入 agentic 树搜索，端到端产出可被 workshop 接收的论文，去除人写模板依赖。",
      tldr_en: "Adds agentic tree search over the v1 pipeline; produces a workshop-accepted paper end-to-end with no human-written template.",
      method: ["Agentic tree search 替代线性 pipeline", "实验管理 agent + VLM 反馈审图", "去除对人写代码模板的依赖"],
      relation: "Skill 4 实验引擎的对照系统；其 tree-search 编排是 run 循环的灵感来源之一。",
      take: ["树搜索可显著扩大实验探索面，但成本高，需预算闸门", "VLM 审图可纳入 report 阶段做图表质检"],
      featured: true,
    },
    {
      slug: "ai-scientist-v1", arxiv: "2408.06292", v: 2, kind: "paper",
      title: "The AI Scientist: Towards Fully Automated Open-Ended Scientific Discovery",
      authors: ["Lu, C.", "Lu, C.", "Lange, R. T.", "Foerster, J.", "Clune, J.", "Ha, D."],
      published: "2024-08-12", ingested: "2026-05-28",
      tags: ["agentic-research", "idea-generation", "automated-review"],
      relevance: 0.88, status: "summarized", cites: [],
      tldr_zh: "首个 idea→实验→写作→评审的端到端自动科研 pipeline，含 LLM 自动审稿器。",
      tldr_en: "First end-to-end idea→experiment→writing→review pipeline for automated science, with an LLM reviewer.",
      method: ["Idea 生成 + novelty 检索", "实验模板 + 迭代执行", "自动 LaTeX 写作", "LLM 审稿器近人类一致性"],
      relation: "整体链路的母本；但其自评机制被后续工作（2409.04109）批评为偏差来源。",
      take: ["generator≠reviewer 必须硬分离", "引用真实性是写作阶段高频失败点"],
    },
    {
      slug: "llm-novel-ideas-human-study", arxiv: "2409.04109", v: 1, kind: "paper",
      title: "Can LLMs Generate Novel Research Ideas? A Large-Scale Human Study with 100+ NLP Researchers",
      authors: ["Si, C.", "Yang, D.", "Hashimoto, T."],
      published: "2024-09-06", ingested: "2026-05-27",
      tags: ["idea-generation", "human-eval", "novelty"],
      relevance: 0.94, status: "summarized", cites: [],
      tldr_zh: "大规模人评：LLM idea 在新颖性上可超人类，但可行性被系统性高估，自评不可靠。",
      tldr_en: "Large human study: LLM ideas can beat humans on novelty but feasibility is systematically over-rated; self-eval is unreliable.",
      method: ["100+ NLP 研究者盲评", "新颖性 vs 可行性分离测量", "揭示 LLM 自评偏差"],
      relation: "本系统两条硬约束的来源：可行性虚高 → 人在环闸门；禁止自评 → generator≠reviewer。",
      take: ["candidate→accepted 设为唯一人工闸门", "可行性分数需折扣看待"],
      featured: true,
    },
    {
      slug: "nova-novelty-diversity", arxiv: "2410.14255", v: 1, kind: "paper",
      title: "Nova: An Iterative Planning Approach to Enhance Novelty and Diversity of LLM-Generated Ideas",
      authors: ["Hu, X.", "Fu, Y.", "Luo, H.", "et al."],
      published: "2024-10-18", ingested: "2026-05-26",
      tags: ["idea-generation", "diversity", "planning"],
      relevance: 0.86, status: "summarized", cites: ["2409.04109"],
      tldr_zh: "用迭代规划 + 检索显式提升 idea 的新颖性与多样性，缓解多样性坍缩。",
      tldr_en: "Iterative planning + retrieval to explicitly boost novelty and diversity, mitigating mode collapse.",
      method: ["规划式检索扩展灵感源", "显式多样性目标", "迭代精炼"],
      relation: "idea-forge 的多样性机制（Elo + 语义聚类去重）直接对标其结论。",
      take: ["必须有显式多样性机制，否则坍缩到少数模板"],
    },
    {
      slug: "google-co-scientist", arxiv: null, v: 1, kind: "paper",
      title: "Towards an AI Co-Scientist (Google DeepMind)",
      authors: ["Gottweis, J.", "Natarajan, V.", "et al. (Google DeepMind)"],
      published: "2025-02-19", ingested: "2026-05-25",
      tags: ["multi-agent", "elo-tournament", "hypothesis"],
      relevance: 0.89, status: "summarized", cites: [],
      tldr_zh: "多 agent 系统用 Elo 锦标赛对假设排序，generate/reflect/rank/evolve 循环。",
      tldr_en: "Multi-agent system ranking hypotheses by an Elo tournament; generate/reflect/rank/evolve loop.",
      method: ["Elo 锦标赛排序假设", "多 agent 辩论", "演化式精炼"],
      relation: "idea-forge 排序阶段采用 Elo 锦标赛而非单点打分，直接源于此。",
      take: ["用锦标赛相对排序，规避单点打分噪声"],
    },
    {
      slug: "agent-laboratory", arxiv: "2501.04227", v: 1, kind: "paper",
      title: "Agent Laboratory: Using LLM Agents as Research Assistants",
      authors: ["Schmidgall, S.", "et al. (AMD / JHU)"],
      published: "2025-01-08", ingested: "2026-05-24",
      tags: ["agentic-research", "experiment", "baseline"],
      relevance: 0.82, status: "summarized", cites: ["2408.06292"],
      tldr_zh: "把研究拆成文献综述→实验→报告三阶段，强调真实可复现的 baseline。",
      tldr_en: "Decomposes research into literature→experiment→report; emphasizes real reproducible baselines.",
      method: ["三阶段流水线", "人在环可介入", "可测主指标 + baseline"],
      relation: "Skill 4 plan 阶段「先定指标与基线再跑」与之一致。",
      take: ["实验必须有可测主指标 + 真实复现 baseline"],
    },
    {
      slug: "sakana-independent-eval", arxiv: "2502.14297", v: 1, kind: "paper",
      title: "An Independent Assessment of Automated Research Idea Generation Systems",
      authors: ["(Independent evaluation)"],
      published: "2025-02-20", ingested: "2026-05-23",
      tags: ["evaluation", "novelty", "citation-graph"],
      relevance: 0.80, status: "summarized", cites: ["2408.06292", "2409.04109"],
      tldr_zh: "独立评测指出纯关键词查重不可靠，应走 citation graph 判定新颖性。",
      tldr_en: "Independent eval: keyword-only novelty checks are unreliable; use the citation graph instead.",
      method: ["复现多套 idea 系统", "查重方法对比", "揭示关键词查重漏判"],
      relation: "reviewer 查重强制调 Semantic Scholar citation graph，禁纯关键词，源于此。",
      take: ["查重必须走 citation graph，不能只靠关键词"],
    },
    {
      slug: "researchagent", arxiv: "2404.07738", v: 1, kind: "paper",
      title: "ResearchAgent: Iterative Research Idea Generation over Scientific Literature with LLMs",
      authors: ["Baek, J.", "Jauhar, S. K.", "et al."],
      published: "2024-04-11", ingested: "2026-05-22",
      tags: ["idea-generation", "literature", "knowledge-graph"],
      relevance: 0.78, status: "summarized", cites: [],
      tldr_zh: "基于文献图谱迭代生成 idea，引入多 reviewing agent 反馈精炼。",
      tldr_en: "Iteratively generates ideas over a literature graph with multiple reviewing agents refining them.",
      method: ["实体中心文献图", "多 reviewing agent 反馈", "迭代精炼"],
      relation: "librarian 的 concept 实体页 + 交叉链接与其图谱思路相通。",
      take: ["概念实体页能提升 idea 接地质量"],
    },
    {
      slug: "sciagents", arxiv: "2409.05556", v: 1, kind: "paper",
      title: "SciAgents: Automating Scientific Discovery through Multi-agent Graph Reasoning",
      authors: ["Ghafarollahi, A.", "Buehler, M. J."],
      published: "2024-09-09", ingested: "2026-05-21",
      tags: ["multi-agent", "knowledge-graph", "discovery"],
      relevance: 0.74, status: "ingested", cites: [],
      tldr_zh: "在本体知识图上多 agent 推理，组合式发现跨域研究假设。",
      tldr_en: "Multi-agent reasoning over an ontological knowledge graph for compositional cross-domain hypotheses.",
      method: ["本体知识图", "多 agent 图推理", "组合式假设生成"],
      relation: "跨方向 idea（cross_direction）的组合式生成可借鉴。",
      take: ["图上组合可催生跨域 idea"],
    },
  ];

  // ---- Surveys ----
  const surveys = [
    { slug: "survey-llm4science", title: "A Survey on LLM-based Autonomous Agents for Science", relevance: 0.84, papers: 142 },
    { slug: "survey-auto-ml-research", title: "Automated Research: From AutoML to Autonomous Discovery", relevance: 0.79, papers: 96 },
  ];

  // ---- Concepts (concepts/<slug>.md entity pages, per vault) ----
  const concepts = [
    // — llm-agentic-research —
    { slug: "elo-tournament", dir: "llm-agentic-research", kind: "method", title: "Elo 锦标赛排序", titleEn: "Elo tournament ranking",
      def: "用成对比较的相对评分代替单点绝对打分，对一组 idea / 假设做锦标赛式排序，规避单点打分的噪声。",
      refs: ["google-co-scientist"], related: ["semantic-dedup", "novelty-hallucination"], usedInIdeas: ["I-20260529-001"], n: 6 },
    { slug: "citation-graph-novelty", dir: "llm-agentic-research", kind: "method", title: "Citation-graph 查重", titleEn: "Citation-graph novelty check",
      def: "通过被引 / 共引子图而非纯关键词，判断一条 idea 与现有工作的真实距离，抓“看似新实则已有”的幻觉新颖性。",
      refs: ["sakana-independent-eval"], related: ["novelty-hallucination"], usedInIdeas: ["I-20260529-003"], n: 4 },
    { slug: "agentic-tree-search", dir: "llm-agentic-research", kind: "architecture", title: "Agentic 树搜索", titleEn: "Agentic tree search",
      def: "用树搜索而非线性 pipeline 编排实验探索，显著扩大探索面，但成本高、需预算闸门约束。",
      refs: ["ai-scientist-v2"], related: ["baseline-reproduction"], usedInIdeas: [], n: 3 },
    { slug: "baseline-reproduction", dir: "llm-agentic-research", kind: "methodology", title: "Baseline 复现策略", titleEn: "Baseline reproduction policy",
      def: "按“官方代码 > 第三方可信复现 > 自重写 > 仅引用论文数字（标注非公平）”的优先级复现对比方法，并设单 baseline 预算上限。",
      refs: ["agent-laboratory", "ai-scientist-v2"], related: ["agentic-tree-search"], usedInIdeas: ["I-20260528-007"], n: 5 },
    { slug: "novelty-hallucination", dir: "llm-agentic-research", kind: "problem", title: "新颖性幻觉", titleEn: "Novelty hallucination",
      def: "idea 在 embedding 相似度上看似新颖、实则在引用图中已有等价工作的现象，是 idea 生成系统的高频失败模式。",
      refs: ["llm-novel-ideas-human-study", "nova"], related: ["citation-graph-novelty", "semantic-dedup"], usedInIdeas: ["I-20260529-003"], n: 7 },
    { slug: "semantic-dedup", dir: "llm-agentic-research", kind: "method", title: "语义聚类去重", titleEn: "Semantic-cluster dedup",
      def: "用 embedding 聚类对存活 idea 去重，每簇只保留 Elo 最高者，防止高分 idea 同质化导致多样性坍缩。",
      refs: ["nova"], related: ["elo-tournament"], usedInIdeas: ["I-20260529-001"], n: 4 },
    { slug: "human-in-the-loop-gate", dir: "llm-agentic-research", kind: "methodology", title: "人在环闸门", titleEn: "Human-in-the-loop gate",
      def: "把不可逆 / 高风险动作（算力预算、远程写、idea 晋级、论文投稿）统一收敛到人工确认闸门，异步队列不阻塞其它方向。",
      refs: ["llm-novel-ideas-human-study"], related: [], usedInIdeas: ["I-20260527-002"], n: 5 },
    // — diffusion-rl —
    { slug: "diffusion-policy-concept", dir: "diffusion-rl", kind: "architecture", title: "扩散策略", titleEn: "Diffusion policy",
      def: "用条件扩散模型表示策略，把动作（序列）生成建模为去噪过程，对多模态动作分布更稳健。",
      refs: ["diffusion-policy"], related: ["classifier-free-guidance"], usedInIdeas: [], n: 5 },
    { slug: "classifier-free-guidance", dir: "diffusion-rl", kind: "method", title: "无分类器引导", titleEn: "Classifier-free guidance",
      def: "训练时随机丢弃条件、推理时线性组合有 / 无条件预测，以可控强度引导生成，广泛用于条件决策扩散。",
      refs: ["decision-diffuser"], related: ["diffusion-policy-concept", "ddpo-finetune"], usedInIdeas: [], n: 4 },
    { slug: "consistency-model", dir: "diffusion-rl", kind: "method", title: "一致性模型", titleEn: "Consistency model",
      def: "学习把任意噪声直接映射回数据流形，实现单步 / 少步采样，缓解扩散策略推理慢的问题。",
      refs: ["consistency-models"], related: [], usedInIdeas: [], n: 3 },
    { slug: "ddpo-finetune", dir: "diffusion-rl", kind: "method", title: "扩散模型 RL 微调", titleEn: "RL fine-tuning of diffusion (DDPO)",
      def: "把去噪过程视作多步 MDP，用策略梯度按下游奖励直接微调扩散模型。",
      refs: ["ddpo"], related: ["classifier-free-guidance"], usedInIdeas: [], n: 4 },
  ];

  // ---- Ideas (full lifecycle spread across statuses) ----
  const ideas = [
    {
      id: "I-20260529-003", title: "用 citation-graph 重排序抑制 idea 生成的新颖性幻觉",
      titleEn: "Citation-graph reranking to suppress novelty hallucination in idea generation",
      origin: "idea-forge", created: "2026-05-29", status: "implemented",
      scores: { novelty: 8.2, feasibility: 7.0, operability: 7.5, impact: 8.0, composite: 7.74 },
      elo: 1576, novelty: { closest: ["2410.14255", "2409.04109"], verdict: "novel-with-caveat" },
      cites: ["sakana-independent-eval", "nova-novelty-diversity", "llm-novel-ideas-human-study"],
      oneLine: "在 idea 排序前，用被引/共引子图重估每条 idea 与最近邻 prior 的真实距离，过滤“看似新但实则已有”的幻觉新颖性。",
      experiment: { workspace: "data/experiments/I-20260529-003", status: "done", best: { name: "novelty-precision", value: 0.83, delta: "+9.4pt" } },
      paper: { workspace: "data/papers/I-20260529-003", venue: "neurips2026", review: "reviewed" },
      featured: true,
    },
    {
      id: "I-20260529-001", title: "Elo 锦标赛 + 语义聚类的多样性保持 idea 生成",
      titleEn: "Diversity-preserving idea generation via Elo tournament + semantic clustering",
      origin: "idea-forge", created: "2026-05-29", status: "accepted",
      scores: { novelty: 7.6, feasibility: 8.1, operability: 8.0, impact: 7.2, composite: 7.66 },
      elo: 1561, novelty: { closest: ["google-co-scientist"], verdict: "novel" },
      cites: ["google-co-scientist", "nova-novelty-diversity"],
      oneLine: "把 Elo 锦标赛与 embedding 聚类联合，每簇保留 Elo 最高者，避免高分 idea 同质化导致多样性坍缩。",
      experiment: { workspace: "data/experiments/I-20260529-001", status: "planned", best: null },
      paper: { workspace: null, venue: null, review: "none" },
    },
    {
      id: "I-20260528-007", title: "树搜索实验编排中的 baseline 自动复现优先级调度",
      titleEn: "Priority scheduling for automatic baseline reproduction in tree-search experiments",
      origin: "idea-forge", created: "2026-05-28", status: "candidate",
      scores: { novelty: 7.9, feasibility: 6.4, operability: 7.1, impact: 7.6, composite: 7.36 },
      elo: 1548, novelty: { closest: ["agent-laboratory", "ai-scientist-v2"], verdict: "novel-with-caveat" },
      cites: ["agent-laboratory", "ai-scientist-v2"],
      oneLine: "按“官方代码>第三方复现>自重写>仅引用数字”的优先级与单 baseline 预算上限，自动调度复现顺序，避免烧光算力。",
      experiment: { workspace: null, status: "none", best: null },
      paper: { workspace: null, venue: null, review: "none" },
    },
    {
      id: "I-20260528-004", title: "VLM 审图反馈纳入论文图表的自动质检闭环",
      titleEn: "Closing the loop: VLM figure critique for automated paper plots",
      origin: "code-to-idea", created: "2026-05-28", status: "candidate",
      scores: { novelty: 7.4, feasibility: 7.8, operability: 7.6, impact: 6.9, composite: 7.36 },
      elo: 1532, novelty: { closest: ["ai-scientist-v2"], verdict: "novel-with-caveat" },
      cites: ["ai-scientist-v2"],
      oneLine: "report 阶段用 VLM 审查生成的指标图（坐标轴/图例/误差棒），把问题回灌 plot.py 直到合格。",
      experiment: { workspace: null, status: "none", best: null },
      paper: { workspace: null, venue: null, review: "none" },
    },
    {
      id: "I-20260527-009", title: "概念实体页驱动的检索规划式 idea 接地",
      titleEn: "Concept-entity-driven retrieval planning for idea grounding",
      origin: "idea-forge", created: "2026-05-27", status: "candidate",
      scores: { novelty: 7.1, feasibility: 8.3, operability: 7.9, impact: 6.7, composite: 7.35 },
      elo: 1521, novelty: { closest: ["researchagent"], verdict: "novel-with-caveat" },
      cites: ["researchagent", "sciagents"],
      oneLine: "generator 先在 concept 页上做检索规划，再展开 idea，使每条 idea 强接地到具体方法实体而非泛泛而谈。",
      experiment: { workspace: null, status: "none", best: null },
      paper: { workspace: null, venue: null, review: "none" },
    },
    {
      id: "I-20260527-002", title: "人在环闸门的异步队列对自动化吞吐的影响建模",
      titleEn: "Modeling async human-gate queues' impact on automation throughput",
      origin: "idea-forge", created: "2026-05-27", status: "candidate",
      scores: { novelty: 6.8, feasibility: 8.0, operability: 7.4, impact: 6.5, composite: 7.04 },
      elo: 1498, novelty: { closest: [], verdict: "novel" },
      cites: ["llm-novel-ideas-human-study"],
      oneLine: "把审批延迟建模为排队系统，量化人闸位置对端到端吞吐与质量的权衡，给出闸门放置策略。",
      experiment: { workspace: null, status: "none", best: null },
      paper: { workspace: null, venue: null, review: "none" },
    },
    {
      id: "I-20260526-011", title: "用纯关键词扩展提升文献召回",
      titleEn: "Boost literature recall via pure keyword expansion",
      origin: "idea-forge", created: "2026-05-26", status: "rejected",
      scores: { novelty: 4.1, feasibility: 8.4, operability: 8.0, impact: 4.0, composite: 5.6 },
      elo: 1402, novelty: { closest: ["sakana-independent-eval"], verdict: "not-novel" },
      cites: ["sakana-independent-eval"],
      oneLine: "（已淘汰）纯关键词扩展召回——与现有工作高度重合，且被 2502.14297 证明不可靠。",
      experiment: { workspace: null, status: "none", best: null },
      paper: { workspace: null, venue: null, review: "none" },
      rejectReason: "novelty verdict = not-novel；composite 5.6 < 7.0 闸门阈值。",
    },
  ];

  // ---- Idea generation run (idea-forge live state) ----
  const forgeRun = {
    direction: "llm-agentic-research", nRaw: 15, rounds: 3, keep: "5-10",
    stage: "elo", // generate | review | elo | dedup | gate | done
    raw: 15, survived: 8, clusters: 6, candidates: 6,
    gateThreshold: 7.0,
    rubricWeights: { novelty: 0.35, feasibility: 0.25, operability: 0.20, impact: 0.20 },
    rounds_log: [
      { round: 1, action: "generator", note: "15 raw ideas，每条 link ≥1 paper" },
      { round: 1, action: "reviewer×3", note: "集成评审 + novelty SS 查重，淘汰 4" },
      { round: 2, action: "generator", note: "读批评 → 反思 refine 11 条" },
      { round: 2, action: "reviewer×3", note: "淘汰 2，11→9" },
      { round: 3, action: "generator", note: "refine 9 条收敛" },
      { round: 3, action: "reviewer×3", note: "最终评分，9→8 存活" },
    ],
  };

  // ---- Elo tournament matchups (idea review) ----
  const eloMatches = [
    { a: "I-20260529-003", b: "I-20260528-007", winner: "a", margin: "decisive", reason: "A 的可测指标更清晰，baseline 路径明确；B 的调度收益依赖未验证假设。" },
    { a: "I-20260529-001", b: "I-20260527-009", winner: "a", margin: "narrow", reason: "两者都强，A 的多样性机制更直接可评测。" },
    { a: "I-20260529-003", b: "I-20260529-001", winner: "a", margin: "narrow", reason: "A 直接攻击新颖性幻觉这一核心痛点，影响面更大。" },
    { a: "I-20260528-004", b: "I-20260527-002", winner: "a", margin: "decisive", reason: "A 有清晰闭环与现成 VLM 工具；B 偏理论建模、落地间接。" },
  ];

  // ---- Experiment (for implemented idea) ----
  const experiment = {
    idea: "I-20260529-003",
    host: "gpu-node-1", scheduler: "slurm", budgetGpuH: 48, usedGpuH: 31.4,
    metric: { name: "novelty-precision", baseline: 0.736, best: 0.830, target: ">0.80" },
    program: {
      goal: "把 idea novelty 判定的 precision 从 ~0.74 提升到 >0.80，不牺牲 recall（>0.70）。",
      hypotheses: [
        { h: "H1: 用 2-hop 共引子图重估距离优于纯 embedding 相似度", status: "confirmed" },
        { h: "H2: 加入被引时间衰减进一步提升 precision", status: "confirmed" },
        { h: "H3: 3-hop 比 2-hop 更好", status: "rejected" },
        { h: "H4: 联合关键词信号有增益", status: "testing" },
      ],
      stop: "预算 48 GPU·h 耗尽 或 precision 连续 3 run 无提升。",
    },
    baselines: [
      { name: "Embedding cosine (Nova-style)", source: "自重写", repro: "done", value: 0.736, note: "公平复现，同评测集" },
      { name: "Keyword overlap", source: "自重写", repro: "done", value: 0.611, note: "下界参照" },
      { name: "AI-Scientist novelty check", source: "官方代码", repro: "done", value: 0.702, note: "官方实现，适配评测集" },
    ],
    runs: [
      { id: "run-001", diff: "baseline: embedding cosine", metric: 0.736, verdict: "keep", ts: "05-28 14:02", gpuH: 2.1 },
      { id: "run-002", diff: "+ 2-hop co-citation subgraph", metric: 0.781, verdict: "keep", ts: "05-28 18:40", gpuH: 4.8 },
      { id: "run-003", diff: "+ citation time-decay", metric: 0.804, verdict: "keep", ts: "05-29 02:11", gpuH: 5.2 },
      { id: "run-004", diff: "3-hop expansion", metric: 0.792, verdict: "discard", ts: "05-29 07:33", gpuH: 6.0 },
      { id: "run-005", diff: "2-hop + decay + degree-norm", metric: 0.830, verdict: "keep", ts: "05-29 13:20", gpuH: 5.4 },
      { id: "run-006", diff: "+ keyword signal fusion", metric: 0.826, verdict: "running", ts: "05-29 19:05", gpuH: 7.9 },
    ],
    ablation: [
      { config: "Full (2-hop + decay + degree-norm)", precision: 0.830, recall: 0.742 },
      { config: "− degree-norm", precision: 0.811, recall: 0.738 },
      { config: "− time-decay", precision: 0.781, recall: 0.745 },
      { config: "− co-citation (embedding only)", precision: 0.736, recall: 0.751 },
    ],
  };

  // ---- Paper (writer) ----
  const paper = {
    idea: "I-20260529-003", venue: "neurips2026", anonymized: true, pageLimit: 9, pages: 8,
    title: "Citation-Graph Reranking Suppresses Novelty Hallucination in Automated Idea Generation",
    sections: [
      { id: "abstract", name: "Abstract", words: 198, status: "done" },
      { id: "intro", name: "1 Introduction", words: 812, status: "done" },
      { id: "related", name: "2 Related Work", words: 640, status: "done" },
      { id: "method", name: "3 Method", words: 1340, status: "done" },
      { id: "experiments", name: "4 Experiments", words: 1180, status: "done" },
      { id: "conclusion", name: "5 Conclusion", words: 320, status: "draft" },
    ],
    bib: [
      { key: "yamada2026aiscientist2", arxiv: "2504.08066", ok: true, title: "The AI Scientist-v2" },
      { key: "lu2024aiscientist", arxiv: "2408.06292", ok: true, title: "The AI Scientist" },
      { key: "si2024novel", arxiv: "2409.04109", ok: true, title: "Can LLMs Generate Novel Research Ideas?" },
      { key: "hu2024nova", arxiv: "2410.14255", ok: true, title: "Nova" },
      { key: "gottweis2025coscientist", arxiv: null, ok: true, title: "Towards an AI Co-Scientist" },
      { key: "indep2025assess", arxiv: "2502.14297", ok: true, title: "Independent Assessment ..." },
    ],
    build: { status: "ok", overfull: 0, undefinedRefs: 0, missingFigs: 0, log: "latexmk: main.pdf (8 pages) ✓" },
  };

  // ---- Paper review ----
  const review = {
    idea: "I-20260529-003", venue: "neurips2026",
    form: {
      lang: [{ kind: "ok", text: "语法、术语一致性、时态检查通过（0 处问题）。" }],
      format: [{ kind: "ok", text: "符合 NeurIPS 模板：8/9 页、匿名、bib 风格正确。" }],
      cites: [
        { key: "yamada2026aiscientist2", exist: "ok", correct: "ok", line: 142, note: "存在性 + 论述一致" },
        { key: "lu2024aiscientist", exist: "ok", correct: "ok", line: 88, note: "存在性 + 论述一致" },
        { key: "si2024novel", exist: "ok", correct: "warn", line: 211, note: "存在，但 §4 把其“可行性高估”结论误述为“新颖性高估”——断章取义。" },
        { key: "ghost2025rerank", exist: "fail", correct: "fail", line: 176, note: "虚构引用：Semantic Scholar 查无此文，疑似幻觉。" },
        { key: "hu2024nova", exist: "ok", correct: "ok", line: 95, note: "存在性 + 论述一致" },
      ],
    },
    peer: {
      reviewers: [
        { id: "R1", soundness: 3, presentation: 4, contribution: 3, rating: 6, conf: 4 },
        { id: "R2", soundness: 3, presentation: 3, contribution: 3, rating: 5, conf: 3 },
        { id: "R3", soundness: 4, presentation: 3, contribution: 3, rating: 6, conf: 4 },
      ],
      meta: { rating: 5.7, recommend: "Borderline accept（leaning accept）" },
      strengths: [
        "针对“新颖性幻觉”这一明确痛点，方法直接、动机清晰。",
        "消融充分：co-citation / time-decay / degree-norm 三项均有独立增益。",
        "baseline 复现公平（官方代码 + 同评测集），+9.4pt 提升可信。",
      ],
      weaknesses: [
        "评测集规模偏小，缺少跨领域泛化实验。",
        "§4 一处引用论述与原文不符（见形式审 cite_verify）。",
        "3-hop 反而下降的现象缺少机理分析。",
      ],
      questions: [
        "degree-norm 对高被引奠基作是否引入偏置？",
        "time-decay 的半衰期超参如何选取，是否敏感？",
      ],
    },
  };

  // ---- Approval gates (human-in-the-loop) ----
  const gates = [
    { id: "G-20260529-004", type: "paper_submission", skill: "paper-writer", idea: "I-20260529-003", status: "pending",
      title: "投稿 NeurIPS 2026", desc: "main.pdf 已编译（8 页）、bib 全部 SS 校验通过、形式审有 2 处引用问题待修。", created: "2026-05-29 19:40", urgent: true,
      payload: ["venue: neurips2026", "anonymized: true", "deadline: 2026-05-22 (已过 → workshop track)"] },
    { id: "G-20260529-002", type: "idea_promotion", skill: "idea-forge", idea: "I-20260529-001", status: "pending",
      title: "晋级 idea 为 accepted", desc: "Elo 排序 #2 候选，composite 7.66。晋级后进入 experiment-lab。", created: "2026-05-29 16:12", urgent: false,
      payload: ["elo: 1561", "composite: 7.66", "next: experiment-lab plan"] },
    { id: "G-20260529-003", type: "compute_budget", skill: "experiment-lab", idea: "I-20260529-001", status: "pending",
      title: "算力预算确认 48 GPU·h", desc: "plan 已就绪：3 个 baseline、主指标 diversity@k。消耗真实算力前需确认。", created: "2026-05-29 17:05", urgent: false,
      payload: ["host: gpu-node-1", "scheduler: slurm", "budget: 48 GPU·h", "per-baseline cap: 8 GPU·h"] },
    { id: "G-20260529-001", type: "remote_write", skill: "experiment-lab", idea: "I-20260529-003", status: "approved",
      title: "远程写操作：提交 run-006 作业", desc: "向 gpu-node-1 rsync code/ 并 sbatch 提交。只读探查已自动放行。", created: "2026-05-29 18:50", urgent: false,
      decidedBy: "you", decidedAt: "2026-05-29 18:52",
      payload: ["host: gpu-node-1", "op: rsync + sbatch", "isolated: conda env exp-003"] },
    { id: "G-20260528-006", type: "pr_push", skill: "idea-to-pr", idea: "I-20260528-007", status: "approved",
      title: "推送 PR 到 open-source-proj", desc: "1 个改进、过测试、benchmark 不退化。", created: "2026-05-28 11:20", urgent: false,
      decidedBy: "you", decidedAt: "2026-05-28 12:01",
      payload: ["repo: open-source-proj", "branch: feat/baseline-sched", "tests: 142 passed"] },
  ];

  // ---- Activity log ----
  const activity = [
    { t: "19:05", skill: "experiment-lab", icon: "flask", text: "run-006 已提交至 gpu-node-1（slurm job 88213），流式拉日志中", live: true },
    { t: "19:40", skill: "approvals", icon: "gate", text: "新审批：投稿 NeurIPS 2026 待你确认", gate: true },
    { t: "18:52", skill: "approvals", icon: "gate", text: "你已批准远程写操作 G-20260529-001" },
    { t: "13:20", skill: "experiment-lab", icon: "flask", text: "run-005 keep：novelty-precision 0.830（+9.4pt vs baseline）" },
    { t: "11:30", skill: "paper-review", icon: "shield", text: "cite_verify 抓到 1 处虚构引用、1 处断章取义" },
    { t: "09:14", skill: "research-wiki", icon: "book", text: "ingest 日更：新增 3 篇相关论文，水位 → 2026-05-29" },
    { t: "08:40", skill: "idea-forge", icon: "bulb", text: "run 完成：8 存活 → 6 candidate 入库" },
  ];

  // ---- tag dir1 papers + append a second initialized vault's papers ----
  papers.forEach(p => { if (!p.dir) p.dir = "llm-agentic-research"; });
  surveys.forEach(s => { if (!s.dir) s.dir = "llm-agentic-research"; });
  const dir2Papers = [
    { slug: "diffusion-policy", arxiv: "2303.04137", v: 1, kind: "paper", dir: "diffusion-rl",
      title: "Diffusion Policy: Visuomotor Policy Learning via Action Diffusion",
      authors: ["Chi, C.", "Feng, S.", "Du, Y.", "et al."], published: "2023-03-07", ingested: "2026-05-26",
      tags: ["diffusion-policy", "imitation", "manipulation"], relevance: 0.9, status: "summarized", cites: [], featured: true,
      tldr_zh: "用条件扩散模型表示视觉运动策略，把动作生成建模为去噪过程，对多模态动作分布显著更稳健。",
      tldr_en: "Represents visuomotor policies with a conditional diffusion model; models action generation as iterative denoising.",
      method: ["动作序列上的条件去噪", "receding-horizon 滚动控制", "对多模态示范更稳健"],
      relation: "本方向的代表性架构基线，是后续 idea 的接地点。", take: ["扩散策略对多模态 demo 更稳健，可作默认 backbone"] },
    { slug: "diffuser", arxiv: "2205.09991", v: 1, kind: "paper", dir: "diffusion-rl",
      title: "Planning with Diffusion for Flexible Behavior Synthesis",
      authors: ["Janner, M.", "Du, Y.", "Tenenbaum, J.", "Levine, S."], published: "2022-05-20", ingested: "2026-05-25",
      tags: ["planning", "trajectory", "diffusion"], relevance: 0.85, status: "summarized", cites: [],
      tldr_zh: "把轨迹规划建模为对整条轨迹去噪，用引导实现灵活的行为合成。",
      tldr_en: "Casts trajectory planning as denoising whole trajectories, with guidance for flexible behavior synthesis.",
      method: ["整条轨迹上的扩散", "基于梯度的引导规划", "长程一致性更好"],
      relation: "Diffuser 是扩散规划的奠基作，雪球检索纳入。", take: ["轨迹级扩散 vs 动作级扩散是关键设计选择"] },
    { slug: "decision-diffuser", arxiv: "2211.15657", v: 1, kind: "paper", dir: "diffusion-rl",
      title: "Is Conditional Generative Modeling All You Need for Decision-Making?",
      authors: ["Ajay, A.", "Du, Y.", "Gupta, A.", "et al."], published: "2022-11-28", ingested: "2026-05-24",
      tags: ["decision-diffuser", "classifier-free-guidance", "offline-rl"], relevance: 0.82, status: "summarized", cites: ["2205.09991"],
      tldr_zh: "用无分类器引导的条件扩散直接做决策，把回报 / 约束作为条件。",
      tldr_en: "Conditional diffusion with classifier-free guidance for decision-making, conditioning on returns/constraints.",
      method: ["无分类器引导", "回报 / 约束条件化", "组合多个条件"],
      relation: "无分类器引导在决策扩散中的代表用法。", take: ["用 CFG 强度调节探索 / 利用是实用旋钮"] },
    { slug: "consistency-models", arxiv: "2303.01469", v: 1, kind: "paper", dir: "diffusion-rl",
      title: "Consistency Models",
      authors: ["Song, Y.", "Dhariwal, P.", "Chen, M.", "Sutskever, I."], published: "2023-03-02", ingested: "2026-05-23",
      tags: ["consistency-model", "fast-sampling", "generative"], relevance: 0.76, status: "summarized", cites: [],
      tldr_zh: "学习从任意噪声直接映射回数据，实现单步 / 少步高质量采样。",
      tldr_en: "Learns to map any noise directly back to data, enabling one/few-step high-quality sampling.",
      method: ["一致性映射", "蒸馏 / 独立训练两种方式", "单步采样"],
      relation: "为扩散策略推理加速提供路径（雪球检索纳入）。", take: ["少步采样是把扩散策略用于实时控制的关键"] },
    { slug: "ddpo", arxiv: "2305.13301", v: 1, kind: "paper", dir: "diffusion-rl",
      title: "Training Diffusion Models with Reinforcement Learning",
      authors: ["Black, K.", "Janner, M.", "Du, Y.", "et al."], published: "2023-05-22", ingested: "2026-05-22",
      tags: ["ddpo", "rl-finetune", "policy-gradient"], relevance: 0.88, status: "summarized", cites: [],
      tldr_zh: "把去噪过程视作多步 MDP，用策略梯度按下游奖励直接微调扩散模型。",
      tldr_en: "Treats denoising as a multi-step MDP and fine-tunes diffusion models with policy gradients on downstream rewards.",
      method: ["去噪 = 多步 MDP", "DDPO 策略梯度", "按任意奖励微调"],
      relation: "扩散 + RL 交叉的核心工作，本方向主线。", take: ["奖励设计与方差控制是 RL 微调成败关键"] },
    { slug: "diff-rl-survey", arxiv: "2311.01223", v: 1, kind: "survey", dir: "diffusion-rl",
      title: "Diffusion Models for Reinforcement Learning: A Survey",
      authors: ["Zhu, Z.", "et al."], published: "2023-11-02", ingested: "2026-05-21",
      tags: ["survey", "diffusion", "rl"], relevance: 0.8, status: "summarized", cites: ["2205.09991", "2303.04137"],
      tldr_zh: "系统梳理扩散模型在 RL 中作策略 / 规划器 / 数据合成器的三类用法与开放问题。",
      tldr_en: "Surveys diffusion models in RL as policies / planners / data synthesizers, with open problems.",
      method: ["分类法：策略 / 规划 / 合成", "评测与基准梳理", "开放问题清单"],
      relation: "本方向的“地图”综述，冷启动主干。", take: ["survey 的分类法可直接作为 concept 骨架"] },
  ];
  const allPapers = papers.concat(dir2Papers);

  window.DATA = { directions, direction, papers: allPapers, surveys, concepts, ideas, forgeRun, eloMatches, experiment, paper, review, gates, activity };
})();
