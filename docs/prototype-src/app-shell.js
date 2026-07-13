/* ============================================================
   App shell — window chrome · sidebar · router · approval drawer
   ============================================================ */
const { Icon } = window.UI;

const NAV = [
  { key: "dashboard", no: null, icon: "dashboard", zh: "总览", en: "Dashboard" },
];
const NAV_PIPE = [
  { key: "wiki", no: "00", icon: "book", zh: "文献追踪", en: "Research Wiki", skill: "research-wiki" },
  { key: "forge", no: "01", icon: "bulb", zh: "Idea 生成", en: "Idea Forge", skill: "idea-forge" },
  { key: "review", no: "02", icon: "scale", zh: "Idea 评审", en: "Idea Review", skill: "idea-forge" },
  { key: "experiment", no: "03", icon: "flask", zh: "实验搭建", en: "Experiment Lab", skill: "experiment-lab" },
  { key: "writer", no: "04", icon: "pen", zh: "论文撰写", en: "Paper Writer", skill: "paper-writer" },
  { key: "preview", no: "05", icon: "shield", zh: "论文评审", en: "Paper Review", skill: "paper-review" },
];

const CRUMB = {
  dashboard: ["Studio", "总览"], wiki: ["Skill 0", "研究知识库"], forge: ["Skill 1", "Idea 生成"],
  review: ["Skill 1", "Idea 评审"], experiment: ["Skill 4", "实验搭建与自主实验"], writer: ["Skill 5", "论文撰写"], preview: ["Skill 6", "论文评审"],
};

const GATE_TYPE_ZH = { idea_promotion: "Idea 晋级", compute_budget: "算力预算", remote_write: "远程写操作", pr_push: "推送 PR", paper_submission: "论文投稿" };

function GateCard({ g, expanded, onToggle, override, onDecide }) {
  const status = override || g.status;
  return (
    <div className="card" style={{ overflow: "hidden", borderColor: g.urgent && status === "pending" ? "var(--accent-soft-2)" : "var(--border)" }}>
      <div className="card-pad hoverable" onClick={onToggle} style={{ padding: "14px 16px", cursor: "pointer", background: g.urgent && status === "pending" ? "var(--accent-soft)" : "var(--surface)" }}>
        <div className="row gap8" style={{ marginBottom: 6 }}>
          <span className="pill sm" style={{ background: "var(--surface-3)" }}>{GATE_TYPE_ZH[g.type]}</span>
          {status === "pending" ? <span className="pill sm" style={{ background: "var(--warn-bg)", color: "var(--warn-tx)" }}><span className="dot" />待审批</span> : status === "approved" ? <span className="pill sm" style={{ background: "var(--ok-bg)", color: "var(--ok-tx)" }}>已批准</span> : <span className="pill sm" style={{ background: "var(--danger-bg)", color: "var(--danger-tx)" }}>已拒绝</span>}
          <Icon name="chevDown" size={15} style={{ marginLeft: "auto", color: "var(--text-3)", transform: expanded ? "rotate(180deg)" : "none", transition: "transform .2s" }} />
        </div>
        <div style={{ fontSize: 13.5, fontWeight: 650 }}>{g.title}</div>
        <div className="row gap8" style={{ marginTop: 5 }}><span className="mono muted" style={{ fontSize: 10.5 }}>{g.id}</span><span className="mono muted" style={{ fontSize: 10.5 }}>· {g.idea}</span></div>
      </div>
      {expanded && (
        <div style={{ padding: "0 16px 16px", borderTop: "0.5px solid var(--border)" }}>
          <div style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.55, margin: "12px 0" }}>{g.desc}</div>
          <div className="codeblock" style={{ fontSize: 11, marginBottom: 14 }}>{g.payload.map((l, i) => <div key={i}>{l}</div>)}</div>
          {status === "pending" ? (
            <div className="row gap8">
              <button className="btn btn-primary sm" style={{ flex: 1, justifyContent: "center" }} onClick={() => onDecide(g.id, "approved")}><Icon name="check" size={13} />批准 approve</button>
              <button className="btn btn-ghost sm" style={{ flex: 1, justifyContent: "center" }} onClick={() => onDecide(g.id, "rejected")}><Icon name="x" size={13} />拒绝 reject</button>
            </div>
          ) : (
            <div className="row gap8" style={{ fontSize: 11.5, color: "var(--text-3)" }}><Icon name="check" size={13} style={{ color: "var(--ok-tx)" }} />由 {g.decidedBy || "you"} 于 {g.decidedAt || "刚刚"} {status === "approved" ? "批准" : "拒绝"} · 流水线已继续</div>
          )}
        </div>
      )}
    </div>
  );
}

function ApprovalDrawer({ open, focusGate, onClose, overrides, onDecide }) {
  const D = window.DATA;
  const [expanded, setExpanded] = React.useState(focusGate);
  React.useEffect(() => { if (focusGate) setExpanded(focusGate); }, [focusGate, open]);
  if (!open) return null;
  const pending = D.gates.filter(g => (overrides[g.id] || g.status) === "pending");
  const decided = D.gates.filter(g => (overrides[g.id] || g.status) !== "pending");
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <div className="drawer">
        <div className="row" style={{ padding: "18px 20px", borderBottom: "0.5px solid var(--border)", justifyContent: "space-between" }}>
          <div>
            <div className="row gap8"><Icon name="gate" size={18} style={{ color: "var(--accent)" }} /><span style={{ fontSize: 15, fontWeight: 680 }}>审批中心</span></div>
            <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 3 }}>人在环闸门 · approvals/queue.jsonl</div>
          </div>
          <button className="icon-btn" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>
        <div className="scroll" style={{ overflowY: "auto", flex: 1, padding: "16px 18px" }}>
          <div className="row" style={{ marginBottom: 10 }}><span className="sb-section" style={{ padding: 0 }}>待处理 · {pending.length}</span></div>
          <div className="col gap10" style={{ marginBottom: 24 }}>
            {pending.length ? pending.map(g => <GateCard key={g.id} g={g} expanded={expanded === g.id} onToggle={() => setExpanded(expanded === g.id ? null : g.id)} override={overrides[g.id]} onDecide={onDecide} />) : <div className="empty" style={{ padding: 20 }}>没有待处理的审批 🎉</div>}
          </div>
          <div className="row" style={{ marginBottom: 10 }}><span className="sb-section" style={{ padding: 0 }}>历史记录</span></div>
          <div className="col gap10">
            {decided.map(g => <GateCard key={g.id} g={g} expanded={expanded === g.id} onToggle={() => setExpanded(expanded === g.id ? null : g.id)} override={overrides[g.id]} onDecide={onDecide} />)}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-4)", lineHeight: 1.5, marginTop: 20, padding: "0 2px" }}>
            交互式会话用 AskUserQuestion 同步阻塞；异步（cron / loop）写 pending 记录并暂停该 idea 分支，等人 decide 后从断点继续。
          </div>
        </div>
      </div>
    </>
  );
}

function App() {
  const [view, setView] = React.useState("dashboard");
  const [currentDir, setCurrentDir] = React.useState("llm-agentic-research");
  const [focusIdea, setFocusIdea] = React.useState(null);
  const [drawer, setDrawer] = React.useState(false);
  const [focusGate, setFocusGate] = React.useState(null);
  const [overrides, setOverrides] = React.useState({});

  const D = window.DATA;
  const pendingCount = D.gates.filter(g => (overrides[g.id] || g.status) === "pending").length;

  function go(v, idea) { setView(v); if (idea) setFocusIdea(idea); window.__scrollTop && window.__scrollTop(); }
  function openGates(gateId) { setFocusGate(gateId); setDrawer(true); }
  function decide(id, status) { setOverrides(o => ({ ...o, [id]: status })); }

  const contentRef = React.useRef(null);
  React.useEffect(() => { if (contentRef.current) contentRef.current.scrollTop = 0; }, [view]);

  function navItem(n, isPipe) {
    const active = view === n.key;
    return (
      <div key={n.key} className={"nav-item" + (active ? " active" : "")} onClick={() => go(n.key)}>
        {isPipe ? <span className="stage-no mono">{n.no}</span> : null}
        <span className="nav-ic"><Icon name={n.icon} size={16} /></span>
        <span style={{ flex: 1 }}>{n.zh}</span>
      </div>
    );
  }

  const [c1, c2] = CRUMB[view];

  return (
    <div className="desktop">
      <div className="win">
        {/* sidebar */}
        <div className="sidebar">
          <div className="sb-top"><div className="lights"><i className="r" /><i className="y" /><i className="g" /></div></div>
          <div className="sb-brand">
            <div className="sb-logo"><Icon name="sparkle" size={15} style={{ color: "#fff" }} /></div>
            <div><h1>Auto-Research</h1><p>{currentDir}</p></div>
          </div>
          <div className="sb-scroll scroll">
            {NAV.map(n => navItem(n, false))}
            <div className="sb-section">研究流水线 · Pipeline</div>
            {NAV_PIPE.map(n => navItem(n, true))}
          </div>
          <div className="sb-foot">
            <div className="av">YK</div>
            <div style={{ flex: 1, minWidth: 0 }}><div style={{ fontSize: 12, fontWeight: 600 }}>研究员</div><div style={{ fontSize: 10.5, color: "var(--text-3)" }}>Researcher</div></div>
            <button className="icon-btn" style={{ width: 28, height: 28, border: "none", background: "transparent" }}><Icon name="settings" size={16} /></button>
          </div>
        </div>

        {/* main */}
        <div className="main">
          <div className="topbar">
            <div className="crumb"><span>{c1}</span><span className="sep">›</span><b>{c2}</b></div>
            <div className="spacer" />
            <div className="searchbox"><Icon name="search" size={14} /><span>搜索论文 / idea / 实验…</span><span className="mono" style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-4)" }}>⌘K</span></div>
            <button className="icon-btn" onClick={() => openGates(null)} title="审批中心">
              <Icon name="bell" size={16} />
              {pendingCount > 0 && <span className="badge">{pendingCount}</span>}
            </button>
          </div>
          <div className="content scroll" ref={contentRef}>
            {view === "dashboard" && <window.Dashboard go={go} openGates={openGates} />}
            {view === "wiki" && <window.ResearchWiki currentDir={currentDir} setCurrentDir={setCurrentDir} />}
            {view === "forge" && <window.IdeaForge go={go} />}
            {view === "review" && <window.IdeaReview focusIdea={focusIdea} openGates={openGates} />}
            {view === "experiment" && <window.ExperimentLab openGates={openGates} />}
            {view === "writer" && <window.PaperWriter openGates={openGates} />}
            {view === "preview" && <window.PaperReview />}
          </div>
        </div>

        <ApprovalDrawer open={drawer} focusGate={focusGate} onClose={() => setDrawer(false)} overrides={overrides} onDecide={decide} />
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
