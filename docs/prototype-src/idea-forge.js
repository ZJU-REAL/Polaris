/* ============================================================
   Idea Forge — generator ⇄ reviewer ⇄ Elo ⇄ dedup ⇄ gate
   ============================================================ */
const { Icon: I3, StatusPill: SP3, PageHead: PH3, ScoreRing: SR3, Wiki: W3 } = window.UI;

const FORGE_STAGES = [
  { key: "generate", zh: "生成", en: "generator", icon: "bulb", desc: "读 index → 钻取 paper 页 → 检索规划式生成 N 个 raw idea，每条 link ≥1 paper" },
  { key: "review", zh: "评审", en: "reviewer ×3", icon: "scale", desc: "集成评审 3 轮：novelty 调 SS 查 closest_prior + 四维打分 + 批评，generator 反思 refine" },
  { key: "elo", zh: "锦标赛", en: "Elo rank", icon: "chart", desc: "存活 idea 两两科学辩论（reviewer 当裁判）→ Elo 排序" },
  { key: "dedup", zh: "去重", en: "cluster dedup", icon: "layers", desc: "embedding 语义聚类，每簇留 Elo 最高，防多样性坍缩" },
  { key: "gate", zh: "闸门", en: "threshold gate", icon: "gate", desc: "composite ≥ 7.0 且 novelty ≠ not-novel → candidate" },
];

function Funnel({ progress }) {
  const D = window.DATA.forgeRun;
  const stack = [
    { label: "raw ideas", n: D.raw, at: 0 },
    { label: "存活 survived", n: D.survived, at: 1 },
    { label: "聚类 clusters", n: D.clusters, at: 3 },
    { label: "candidate", n: D.candidates, at: 4 },
  ];
  return (
    <div className="row gap8" style={{ alignItems: "flex-end", height: 96 }}>
      {stack.map((s, i) => {
        const reached = progress >= s.at;
        const h = 40 + (4 - i) * 14;
        return (
          <React.Fragment key={i}>
            <div className="col" style={{ alignItems: "center", gap: 6, opacity: reached ? 1 : 0.3, transition: "opacity .4s" }}>
              <span className="mono" style={{ fontSize: 20, fontWeight: 700, color: reached ? "var(--accent-text)" : "var(--text-3)" }}>{reached ? s.n : "—"}</span>
              <div style={{ width: 58 - i * 4, height: h, borderRadius: 8, background: reached ? "var(--accent)" : "var(--surface-3)", opacity: reached ? 0.85 - i * 0.12 : 1, transition: "all .4s" }} />
              <span style={{ fontSize: 10, color: "var(--text-3)" }}>{s.label}</span>
            </div>
            {i < stack.length - 1 && <I3 name="chevron" size={16} style={{ color: "var(--text-4)", marginBottom: 30 }} />}
          </React.Fragment>
        );
      })}
    </div>
  );
}

function CandidateCard({ idea, go }) {
  const v = idea.novelty.verdict;
  const vColor = v === "novel" ? "var(--ok-tx)" : v === "not-novel" ? "var(--danger-tx)" : "var(--warn-tx)";
  const vBg = v === "novel" ? "var(--ok-bg)" : v === "not-novel" ? "var(--danger-bg)" : "var(--warn-bg)";
  return (
    <div className="card card-pad hoverable" onClick={() => go("review", idea.id)}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
        <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>{idea.id}</span>
        <SP3 status={idea.status} sm />
      </div>
      <div style={{ fontSize: 14.5, fontWeight: 650, lineHeight: 1.35, marginBottom: 4 }}>{idea.title}</div>
      <div style={{ fontSize: 11.5, color: "var(--text-3)", marginBottom: 12 }}>{idea.titleEn}</div>
      <div style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.5, marginBottom: 14 }}>{idea.oneLine}</div>
      <div className="row gap16" style={{ alignItems: "center" }}>
        <SR3 value={idea.scores.composite} size={42} />
        <div style={{ flex: 1 }}>
          <div className="row gap10 wrap" style={{ fontSize: 11 }}>
            {[["新颖", idea.scores.novelty], ["可行", idea.scores.feasibility], ["可操作", idea.scores.operability], ["影响", idea.scores.impact]].map(([k, val]) => (
              <span key={k} style={{ color: "var(--text-3)" }}>{k} <b className="mono" style={{ color: "var(--text)", fontWeight: 700 }}>{val}</b></span>
            ))}
          </div>
          <div className="row gap8" style={{ marginTop: 8 }}>
            <span className="pill sm" style={{ background: vBg, color: vColor }}>{v}</span>
            <span className="mono" style={{ fontSize: 10.5, color: "var(--text-3)" }}>Elo {idea.elo}</span>
            <span className="row gap6" style={{ marginLeft: "auto" }}>{idea.cites.slice(0, 1).map(c => <W3 key={c}>{c}</W3>)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function IdeaForge({ go }) {
  const D = window.DATA;
  const run = D.forgeRun;
  const [progress, setProgress] = React.useState(4); // 0..4 stage index reached
  const [running, setRunning] = React.useState(false);
  const candidates = D.ideas.filter(i => i.status === "candidate").sort((a, b) => b.elo - a.elo);

  function runForge() {
    setRunning(true); setProgress(-1);
    let s = 0;
    const tick = () => {
      setProgress(s);
      if (s < FORGE_STAGES.length - 1) { s++; setTimeout(tick, 850); }
      else { setRunning(false); }
    };
    setTimeout(tick, 300);
  }

  return (
    <div className="page fadeup">
      <PH3 eyebrow="Skill 1 · idea-forge" title="Idea 生成" en="Idea Forge"
        sub="文献 → idea 的核心循环。generator 与 reviewer 严格分离，Elo 锦标赛排序 + 语义聚类去重，闸门只放高分且确有新颖性的 idea 入库。"
        right={<button className="btn btn-primary" onClick={runForge} disabled={running}>{running ? <><I3 name="refresh" size={14} className="spin" />运行中…</> : <><I3 name="play" size={14} />运行 idea-forge</>}</button>} />

      {/* stage pipeline */}
      <div className="card card-pad" style={{ marginBottom: 22 }}>
        <div className="row" style={{ gap: 0, marginBottom: 22 }}>
          {FORGE_STAGES.map((s, i) => {
            const reached = progress >= i;
            const cur = running && progress === i;
            return (
              <React.Fragment key={s.key}>
                <div style={{ flex: 1, textAlign: "center", opacity: reached ? 1 : 0.4, transition: "opacity .3s" }}>
                  <div style={{ width: 40, height: 40, margin: "0 auto 8px", borderRadius: 11, background: reached ? "var(--accent)" : "var(--surface-2)", color: reached ? "#fff" : "var(--text-3)", display: "flex", alignItems: "center", justifyContent: "center", boxShadow: cur ? "0 0 0 4px var(--accent-soft)" : "none", transition: "all .3s" }} className={cur ? "pulse" : ""}>
                    <I3 name={s.icon} size={19} />
                  </div>
                  <div style={{ fontSize: 12.5, fontWeight: 650 }}>{s.zh}</div>
                  <div className="mono" style={{ fontSize: 9.5, color: "var(--text-3)" }}>{s.en}</div>
                </div>
                {i < FORGE_STAGES.length - 1 && <div style={{ flexShrink: 0, width: 24, alignSelf: "flex-start", marginTop: 20 }}><svg width="24" height="10" viewBox="0 0 24 10"><path d="M0 5h18M15 1l5 4-5 4" fill="none" stroke={progress > i ? "var(--accent)" : "var(--border-strong)"} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg></div>}
              </React.Fragment>
            );
          })}
        </div>
        <div className="card-pad" style={{ background: "var(--surface-2)", borderRadius: 10, fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.5 }}>
          <span className="mono" style={{ color: "var(--accent-text)", fontSize: 11 }}>{progress >= 0 && progress < FORGE_STAGES.length ? FORGE_STAGES[Math.min(progress, 4)].en : "ready"} ›</span>{" "}
          {progress >= 0 && progress < FORGE_STAGES.length ? FORGE_STAGES[Math.min(progress, 4)].desc : "点击运行启动生成循环。"}
        </div>
      </div>

      <div className="row gap20" style={{ alignItems: "stretch", marginBottom: 22 }}>
        <div className="card card-pad" style={{ flex: 1.3 }}>
          <span className="section-h" style={{ marginBottom: 16 }}><I3 name="layers" size={15} style={{ color: "var(--accent)" }} />收敛漏斗 <span className="en-label" style={{ fontSize: 11 }}>15 raw → 6 candidate</span></span>
          <Funnel progress={progress} />
        </div>
        <div className="card card-pad" style={{ flex: 1 }}>
          <span className="section-h" style={{ marginBottom: 14 }}><I3 name="scale" size={15} style={{ color: "var(--accent)" }} />评分 rubric</span>
          {[["novelty 新颖性", run.rubricWeights.novelty], ["feasibility 可行性", run.rubricWeights.feasibility], ["operability 可操作性", run.rubricWeights.operability], ["impact 影响力", run.rubricWeights.impact]].map(([k, w]) => (
            <div key={k} className="row gap10" style={{ marginBottom: 9 }}>
              <span style={{ fontSize: 12, width: 130, flexShrink: 0 }}>{k}</span>
              <div className="bar" style={{ flex: 1 }}><i style={{ width: `${w * 100 / 0.35 * 0.8}%` }} /></div>
              <span className="mono" style={{ fontSize: 11, color: "var(--text-3)", width: 34, textAlign: "right" }}>{w.toFixed(2)}</span>
            </div>
          ))}
          <div className="hr" style={{ margin: "12px 0" }} />
          <div className="row" style={{ justifyContent: "space-between", fontSize: 11.5 }}>
            <span className="muted">闸门阈值 composite ≥</span><span className="mono" style={{ fontWeight: 700 }}>{run.gateThreshold}</span>
          </div>
        </div>
      </div>

      {/* rounds log */}
      <div className="card" style={{ marginBottom: 22, overflow: "hidden" }}>
        <div className="card-pad" style={{ paddingBottom: 8 }}><span className="section-h"><I3 name="refresh" size={15} style={{ color: "var(--accent)" }} />generator ⇄ reviewer 迭代日志 <span className="en-label" style={{ fontSize: 11 }}>3 rounds</span></span></div>
        <div style={{ padding: "4px 18px 14px" }}>
          {run.rounds_log.map((r, i) => (
            <div key={i} className="row gap12" style={{ padding: "8px 0", borderBottom: i < run.rounds_log.length - 1 ? "0.5px solid var(--border)" : "none" }}>
              <span className="mono" style={{ fontSize: 11, color: "var(--text-4)", width: 56, flexShrink: 0 }}>round {r.round}</span>
              <span className="pill sm" style={{ background: r.action.startsWith("generator") ? "var(--accent-soft)" : "var(--info-bg)", color: r.action.startsWith("generator") ? "var(--accent-text)" : "var(--info-tx)", width: 92, justifyContent: "center", flexShrink: 0 }}>{r.action}</span>
              <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>{r.note}</span>
            </div>
          ))}
        </div>
      </div>

      {/* candidates */}
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
        <span className="section-h"><I3 name="bulb" size={15} style={{ color: "var(--accent)" }} />产出 candidate <span className="en-label" style={{ fontSize: 11 }}>{candidates.length} ideas · Elo 排序</span></span>
        <span className="muted" style={{ fontSize: 12 }}>点击进入 idea 评审 →</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        {candidates.map(idea => <CandidateCard key={idea.id} idea={idea} go={go} />)}
      </div>
    </div>
  );
}

window.IdeaForge = IdeaForge;
