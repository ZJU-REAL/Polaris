/* ============================================================
   Dashboard — pipeline overview · stats · gates · activity
   ============================================================ */
const { Icon: I1, StatusPill: SP1, PageHead: PH1, ScoreRing: SR1, Delta: D1, Wiki: W1 } = window.UI;

const PIPELINE = [
  { key: "wiki",       no: "00", icon: "book",  zh: "文献追踪", en: "Research Wiki",  countKey: "papers" },
  { key: "forge",      no: "01", icon: "bulb",  zh: "Idea 生成", en: "Idea Forge",     countKey: "candidates" },
  { key: "review",     no: "02", icon: "scale", zh: "Idea 评审", en: "Idea Review",    countKey: "reviewed" },
  { key: "experiment", no: "03", icon: "flask", zh: "实验搭建", en: "Experiment Lab", countKey: "experiments" },
  { key: "writer",     no: "04", icon: "pen",   zh: "论文撰写", en: "Paper Writer",   countKey: "papers2" },
  { key: "preview",    no: "05", icon: "shield",zh: "论文评审", en: "Paper Review",   countKey: "reviews" },
];

function PipelineFlow({ go }) {
  const D = window.DATA;
  const counts = {
    papers: D.direction.counts.papers,
    candidates: D.ideas.filter(i => i.status === "candidate").length,
    reviewed: D.ideas.filter(i => ["accepted", "implemented", "paper.drafted", "paper.reviewed"].includes(i.status)).length,
    experiments: D.ideas.filter(i => i.experiment.status !== "none").length,
    papers2: D.ideas.filter(i => i.paper.workspace).length,
    reviews: D.ideas.filter(i => i.paper.review === "reviewed").length,
  };
  // which stage the featured idea is currently at
  return (
    <div className="card card-pad" style={{ marginBottom: 24 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 18 }}>
        <div className="row gap10">
          <span className="section-h"><I1 name="layers" size={16} style={{ color: "var(--accent)" }} />端到端研究流水线</span>
          <span className="muted" style={{ fontSize: 12 }}>End-to-end pipeline</span>
        </div>
        <span className="pill"><span className="dot" style={{ background: "var(--ok)" }} />自动运行中 · 方向 {D.direction.titleEn}</span>
      </div>
      <div className="row" style={{ alignItems: "stretch", gap: 0 }}>
        {PIPELINE.map((s, idx) => (
          <React.Fragment key={s.key}>
            <div className="hoverable" onClick={() => go(s.key)} style={{
              flex: 1, borderRadius: 12, padding: "16px 12px 14px", textAlign: "center",
              border: "0.5px solid var(--border)", background: idx === 3 ? "var(--accent-soft)" : "var(--surface-2)",
              position: "relative", cursor: "pointer",
            }}>
              <div style={{ position: "absolute", top: 8, left: 10, fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-4)" }}>{s.no}</div>
              <div style={{ width: 38, height: 38, margin: "4px auto 10px", borderRadius: 10, background: idx === 3 ? "var(--accent)" : "var(--surface)", color: idx === 3 ? "#fff" : "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "var(--shadow-card)" }}>
                <I1 name={s.icon} size={19} />
              </div>
              <div style={{ fontSize: 12.5, fontWeight: 650 }}>{s.zh}</div>
              <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 1 }}>{s.en}</div>
              <div style={{ marginTop: 9, fontFamily: "var(--mono)", fontSize: 20, fontWeight: 700, color: idx === 3 ? "var(--accent-text)" : "var(--text)" }}>{counts[s.countKey]}</div>
              {idx === 3 && <div style={{ position: "absolute", bottom: 8, left: 0, right: 0, fontSize: 9.5, color: "var(--accent-text)", fontWeight: 600 }} className="pulse">● 1 running</div>}
            </div>
            {idx < PIPELINE.length - 1 && (
              <div className="row" style={{ alignItems: "center", width: 26, justifyContent: "center", flexShrink: 0 }}>
                <svg width="22" height="14" viewBox="0 0 22 14"><path d="M1 7h17M14 2l5 5-5 5" fill="none" stroke="var(--border-strong)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </div>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function StatCard({ icon, label, en, value, sub, accent }) {
  return (
    <div className="card card-pad" style={{ flex: 1 }}>
      <div className="row gap10" style={{ marginBottom: 12 }}>
        <div style={{ width: 30, height: 30, borderRadius: 8, background: accent ? "var(--accent-soft)" : "var(--surface-2)", color: accent ? "var(--accent)" : "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center" }}><I1 name={icon} size={16} /></div>
        <div><div style={{ fontSize: 12.5, fontWeight: 600 }}>{label}</div><div style={{ fontSize: 10.5, color: "var(--text-3)" }}>{en}</div></div>
      </div>
      <div className="row" style={{ alignItems: "baseline", gap: 8 }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 28, fontWeight: 700, letterSpacing: "-0.02em" }}>{value}</span>
        {sub && <span style={{ fontSize: 12, color: "var(--text-3)" }}>{sub}</span>}
      </div>
    </div>
  );
}

function ActivityFeed() {
  const D = window.DATA;
  const map = { flask: "flask", gate: "gate", book: "book", bulb: "bulb", shield: "shield" };
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div className="card-pad" style={{ paddingBottom: 12 }}>
        <span className="section-h"><I1 name="clock" size={15} style={{ color: "var(--accent)" }} />近期活动 <span className="en-label" style={{ fontSize: 11 }}>Activity</span></span>
      </div>
      <div style={{ padding: "0 6px 8px" }}>
        {D.activity.map((a, i) => (
          <div key={i} className="row gap12" style={{ padding: "9px 16px", alignItems: "flex-start" }}>
            <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-3)", width: 34, flexShrink: 0, paddingTop: 1 }}>{a.t}</div>
            <div style={{ width: 24, height: 24, borderRadius: 7, flexShrink: 0, background: a.gate ? "var(--accent-soft)" : "var(--surface-2)", color: a.gate ? "var(--accent)" : "var(--text-3)", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <I1 name={map[a.icon] || "dot"} size={13} />
            </div>
            <div style={{ flex: 1, fontSize: 12.5, color: "var(--text)", lineHeight: 1.45, paddingTop: 2 }}>
              {a.text}
              {a.live && <span className="pill sm" style={{ marginLeft: 8, background: "var(--ok-bg)", color: "var(--ok-tx)" }}><span className="dot pulse" />LIVE</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function GatePreview({ openGates }) {
  const D = window.DATA;
  const pending = D.gates.filter(g => g.status === "pending");
  return (
    <div className="card" style={{ overflow: "hidden", borderColor: pending.length ? "var(--accent-soft-2)" : "var(--border)" }}>
      <div className="card-pad row" style={{ justifyContent: "space-between", paddingBottom: 14 }}>
        <span className="section-h"><I1 name="gate" size={15} style={{ color: "var(--accent)" }} />人在环 · 审批中心 <span className="en-label" style={{ fontSize: 11 }}>Approvals</span></span>
        <span className="pill" style={{ background: "var(--accent-soft)", color: "var(--accent-text)" }}>{pending.length} 待处理</span>
      </div>
      <div style={{ padding: "0 14px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
        {pending.map(g => (
          <div key={g.id} className="hoverable" onClick={() => openGates(g.id)} style={{ border: "0.5px solid var(--border)", borderRadius: 10, padding: "12px 14px", background: g.urgent ? "var(--accent-soft)" : "var(--surface-2)" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span style={{ fontSize: 13, fontWeight: 650 }}>{g.title}</span>
              <span className="mono" style={{ fontSize: 10.5, color: "var(--text-3)" }}>{g.type}</span>
            </div>
            <div style={{ fontSize: 11.5, color: "var(--text-2)", marginTop: 5, lineHeight: 1.45 }}>{g.desc}</div>
            <div className="row gap8" style={{ marginTop: 10 }}>
              <button className="btn btn-primary sm" onClick={(e) => { e.stopPropagation(); openGates(g.id); }}><I1 name="check" size={13} />审批</button>
              <span className="mono" style={{ fontSize: 10.5, color: "var(--text-4)", marginLeft: "auto" }}>{g.created}</span>
            </div>
          </div>
        ))}
        <button className="btn btn-soft" onClick={() => openGates(null)} style={{ justifyContent: "center" }}>查看全部审批记录</button>
      </div>
    </div>
  );
}

function FeaturedIdea({ go }) {
  const D = window.DATA;
  const idea = D.ideas.find(i => i.id === "I-20260529-003");
  return (
    <div className="card card-pad hoverable" onClick={() => go("experiment")} style={{ background: "linear-gradient(135deg, var(--surface) 60%, var(--accent-soft) 200%)" }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <span className="pill" style={{ background: "var(--accent)", color: "#fff" }}><I1 name="sparkle" size={12} />当前重点 idea</span>
        <SP1 status={idea.status} sm />
      </div>
      <div style={{ fontSize: 16, fontWeight: 680, letterSpacing: "-0.01em", lineHeight: 1.35 }}>{idea.title}</div>
      <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 4 }}>{idea.titleEn}</div>
      <div className="row gap16" style={{ marginTop: 16, alignItems: "center" }}>
        <SR1 value={idea.scores.composite} label="composite" />
        <div>
          <div style={{ fontSize: 11, color: "var(--text-3)" }}>主指标 novelty-precision</div>
          <div className="row" style={{ alignItems: "baseline", gap: 8 }}>
            <span className="mono" style={{ fontSize: 22, fontWeight: 700 }}>{idea.experiment.best.value}</span>
            <D1>{idea.experiment.best.delta}</D1>
            <span style={{ fontSize: 11, color: "var(--text-3)" }}>vs baseline</span>
          </div>
        </div>
        <div style={{ marginLeft: "auto", textAlign: "right" }}>
          <div className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>Elo {idea.elo}</div>
          <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 4 }}>已贯穿 5 个阶段 →</div>
        </div>
      </div>
    </div>
  );
}

function Dashboard({ go, openGates }) {
  const D = window.DATA;
  const nGates = D.gates.filter(g => g.status === "pending").length;
  return (
    <div className="page fadeup">
      <PH1 eyebrow="Auto-Research Studio" title="总览 Dashboard"
        sub="一个每日自动运行的 AI 自主科研系统：文献 → idea → 评审 → 实验 → 论文 → 评审，知识在 Obsidian LLM Wiki 中复利增长。"
        right={<><button className="btn btn-ghost"><I1 name="refresh" size={15} />同步全部方向</button><button className="btn btn-primary"><I1 name="play" size={14} />运行今日循环</button></>} />

      <PipelineFlow go={go} />

      <div className="row gap16" style={{ marginBottom: 24 }}>
        <StatCard icon="book" label="知识库论文" en="Papers in vault" value={D.direction.counts.papers} sub={`+3 今日`} />
        <StatCard icon="bulb" label="Idea 候选池" en="Idea candidates" value={D.ideas.filter(i => i.status !== "rejected").length} sub="6 candidate" />
        <StatCard icon="flask" label="实验 GPU·h" en="Compute used" value={`${D.experiment.usedGpuH}`} sub={`/ ${D.experiment.budgetGpuH}`} />
        <StatCard icon="gate" label="待审批闸门" en="Pending gates" value={nGates} sub="人在环" accent />
      </div>

      <div className="row gap20" style={{ alignItems: "flex-start" }}>
        <div className="col gap20" style={{ flex: 1.5, minWidth: 0 }}>
          <FeaturedIdea go={go} />
          <ActivityFeed />
        </div>
        <div style={{ flex: 1, minWidth: 0, position: "sticky", top: 0 }}>
          <GatePreview openGates={openGates} />
        </div>
      </div>
    </div>
  );
}

window.Dashboard = Dashboard;
