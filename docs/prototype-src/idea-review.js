/* ============================================================
   Idea Review — Elo tournament · rubric · novelty · promote
   ============================================================ */
const { Icon: I4, StatusPill: SP4, PageHead: PH4, ScoreRing: SR4, Wiki: W4 } = window.UI;

function EloRow({ idea, rank, active, onClick }) {
  const medal = rank <= 3;
  return (
    <div onClick={onClick} style={{
      padding: "12px 16px", cursor: "pointer", borderBottom: "0.5px solid var(--border)",
      background: active ? "var(--surface)" : "transparent", borderLeft: active ? "2px solid var(--accent)" : "2px solid transparent",
    }}>
      <div className="row gap12">
        <span className="mono" style={{ fontSize: 14, fontWeight: 700, color: medal ? "var(--accent-text)" : "var(--text-3)", width: 22, textAlign: "center" }}>{rank}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.3, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{idea.title}</div>
          <div className="row gap8" style={{ marginTop: 4 }}>
            <span className="mono" style={{ fontSize: 10.5, color: "var(--text-3)" }}>{idea.id}</span>
            <SP4 status={idea.status} sm />
          </div>
        </div>
        <div style={{ textAlign: "right", flexShrink: 0 }}>
          <div className="mono" style={{ fontSize: 15, fontWeight: 700, color: "var(--accent-text)" }}>{idea.elo}</div>
          <div className="mono" style={{ fontSize: 10, color: "var(--text-3)" }}>Elo</div>
        </div>
      </div>
    </div>
  );
}

function RubricBar({ k, label, v }) {
  return (
    <div className="row gap10" style={{ marginBottom: 10 }}>
      <span style={{ fontSize: 12, width: 88, flexShrink: 0, color: "var(--text-2)" }}>{label}</span>
      <div className="bar" style={{ flex: 1 }}><i style={{ width: `${v * 10}%`, background: v >= 7.5 ? "var(--ok)" : v >= 6 ? "var(--accent)" : "var(--warn)" }} /></div>
      <span className="mono" style={{ fontSize: 12, fontWeight: 700, width: 28, textAlign: "right" }}>{v}</span>
    </div>
  );
}

function IdeaReviewDetail({ idea, onPromote }) {
  const D = window.DATA;
  if (!idea) return <div className="empty">选择一条 idea 查看评审</div>;
  const matches = D.eloMatches.filter(m => m.a === idea.id || m.b === idea.id);
  const v = idea.novelty.verdict;
  const canPromote = idea.status === "candidate" && v !== "not-novel" && idea.scores.composite >= 7.0;
  return (
    <div className="scroll fadeup" key={idea.id} style={{ overflowY: "auto", height: "100%", padding: "26px 32px 60px" }}>
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>{idea.id}</span>
        <SP4 status={idea.status} sm />
        <span className="pill sm" style={{ background: "var(--surface-3)" }}>origin: {idea.origin}</span>
      </div>
      <h1 style={{ fontSize: 19, fontWeight: 680, lineHeight: 1.3, margin: "2px 0 4px", letterSpacing: "-0.01em" }}>{idea.title}</h1>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>{idea.titleEn}</div>
      <p style={{ fontSize: 13, lineHeight: 1.6, color: "var(--text-2)", margin: "14px 0 0" }}>{idea.oneLine}</p>

      {/* scores + novelty */}
      <div className="row gap16" style={{ marginTop: 22, alignItems: "stretch" }}>
        <div className="card card-pad" style={{ flex: 1.4 }}>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
            <span className="section-h"><I4 name="scale" size={14} style={{ color: "var(--accent)" }} />四维评分</span>
            <SR4 value={idea.scores.composite} size={44} label="composite" />
          </div>
          <RubricBar label="新颖性 novelty" v={idea.scores.novelty} />
          <RubricBar label="可行性 feasib." v={idea.scores.feasibility} />
          <RubricBar label="可操作 operab." v={idea.scores.operability} />
          <RubricBar label="影响力 impact" v={idea.scores.impact} />
        </div>
        <div className="card card-pad" style={{ flex: 1 }}>
          <span className="section-h" style={{ marginBottom: 12 }}><I4 name="shield" size={14} style={{ color: "var(--accent)" }} />Citation-graph 查重</span>
          <div className="pill" style={{ background: v === "novel" ? "var(--ok-bg)" : v === "not-novel" ? "var(--danger-bg)" : "var(--warn-bg)", color: v === "novel" ? "var(--ok-tx)" : v === "not-novel" ? "var(--danger-tx)" : "var(--warn-tx)", marginBottom: 12 }}>
            <span className="dot" />verdict: {v}
          </div>
          <div style={{ fontSize: 11.5, color: "var(--text-3)", marginBottom: 6 }}>最近邻 prior（Semantic Scholar）：</div>
          <div className="row gap6 wrap">
            {idea.novelty.closest.length ? idea.novelty.closest.map(c => <W4 key={c}>{c}</W4>) : <span className="muted" style={{ fontSize: 12 }}>无显著近邻</span>}
          </div>
          <div className="hr" style={{ margin: "14px 0" }} />
          <div style={{ fontSize: 11, color: "var(--text-4)", lineHeight: 1.5 }}>查重走被引/共引子图，非纯关键词（约束源：2502.14297）。</div>
        </div>
      </div>

      {/* matches */}
      <div style={{ marginTop: 22 }}>
        <span className="section-h" style={{ marginBottom: 12 }}><I4 name="chart" size={14} style={{ color: "var(--accent)" }} />Elo 锦标赛 · 科学辩论记录</span>
        {matches.length ? matches.map((m, i) => {
          const opp = m.a === idea.id ? m.b : m.a;
          const won = (m.winner === "a" && m.a === idea.id) || (m.winner === "b" && m.b === idea.id);
          const oppIdea = D.ideas.find(x => x.id === opp);
          return (
            <div key={i} className="card" style={{ padding: "12px 16px", marginBottom: 8, background: "var(--surface)" }}>
              <div className="row gap10" style={{ marginBottom: 6 }}>
                <span className="pill sm" style={{ background: won ? "var(--ok-bg)" : "var(--danger-bg)", color: won ? "var(--ok-tx)" : "var(--danger-tx)" }}>{won ? "WIN" : "LOSS"}</span>
                <span style={{ fontSize: 12, color: "var(--text-3)" }}>vs</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{oppIdea ? oppIdea.title : opp}</span>
                <span className="mono" style={{ fontSize: 10.5, color: "var(--text-4)" }}>{m.margin}</span>
              </div>
              <div style={{ fontSize: 11.5, color: "var(--text-2)", lineHeight: 1.5 }}>裁判：{m.reason}</div>
            </div>
          );
        }) : <div className="muted" style={{ fontSize: 12.5 }}>暂无对局记录</div>}
      </div>

      {/* promote */}
      <div className="card card-pad" style={{ marginTop: 22, background: canPromote ? "var(--accent-soft)" : "var(--surface-2)" }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 650 }}>晋级 idea → accepted</div>
            <div style={{ fontSize: 11.5, color: "var(--text-2)", marginTop: 4, maxWidth: 420 }}>
              {canPromote ? "唯一需要人介入的状态转换（呼应「可行性虚高」）。晋级后进入 experiment-lab。" : idea.status !== "candidate" ? `当前状态 ${idea.status}，无需再晋级。` : "未达闸门（composite < 7.0 或 not-novel），不可晋级。"}
            </div>
          </div>
          <button className="btn btn-primary" disabled={!canPromote} onClick={() => onPromote(idea)} style={{ opacity: canPromote ? 1 : 0.4, alignSelf: "center" }}>
            <I4 name="arrow" size={14} />promote · 人选闸门
          </button>
        </div>
      </div>
    </div>
  );
}

function IdeaReview({ focusIdea, openGates }) {
  const D = window.DATA;
  const ranked = [...D.ideas].sort((a, b) => b.elo - a.elo);
  const [sel, setSel] = React.useState(focusIdea || ranked[0].id);
  React.useEffect(() => { if (focusIdea) setSel(focusIdea); }, [focusIdea]);
  const idea = D.ideas.find(i => i.id === sel);

  return (
    <div className="page-wide" style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ padding: "20px 28px 16px", borderBottom: "0.5px solid var(--border)" }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <div className="h-eyebrow">Skill 1 · idea-forge · review</div>
            <h1 style={{ fontSize: 20, fontWeight: 680, margin: "6px 0 0", letterSpacing: "-0.01em" }}>Idea 评审 <span style={{ fontSize: 14, color: "var(--text-3)", fontWeight: 400 }}>Elo 锦标赛排序</span></h1>
          </div>
          <div className="row gap20">
            <div><div className="mono" style={{ fontSize: 18, fontWeight: 700, textAlign: "right" }}>{D.ideas.filter(i => i.status === "candidate").length}</div><div style={{ fontSize: 11, color: "var(--text-3)" }}>candidate</div></div>
            <div><div className="mono" style={{ fontSize: 18, fontWeight: 700, textAlign: "right" }}>{D.eloMatches.length}</div><div style={{ fontSize: 11, color: "var(--text-3)" }}>对局</div></div>
          </div>
        </div>
      </div>
      <div className="split" style={{ flex: 1, minHeight: 0 }}>
        <div className="split-list" style={{ width: 320 }}>
          <div style={{ padding: "10px 16px", borderBottom: "0.5px solid var(--border)", fontSize: 11, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.04em" }}>Leaderboard · Elo</div>
          <div className="scroll" style={{ overflowY: "auto", flex: 1 }}>
            {ranked.map((idea, i) => <EloRow key={idea.id} idea={idea} rank={i + 1} active={idea.id === sel} onClick={() => setSel(idea.id)} />)}
          </div>
        </div>
        <div className="split-detail">
          <IdeaReviewDetail idea={idea} onPromote={(idea) => openGates("G-20260529-002")} />
        </div>
      </div>
    </div>
  );
}

window.IdeaReview = IdeaReview;
