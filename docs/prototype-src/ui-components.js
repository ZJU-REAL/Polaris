/* ============================================================
   Shared icons + UI primitives  →  window.UI
   ============================================================ */

// Minimal stroke icon set (currentColor)
function Icon({ name, size = 17, sw = 1.6, style }) {
  const p = { fill: "none", stroke: "currentColor", strokeWidth: sw, strokeLinecap: "round", strokeLinejoin: "round" };
  const paths = {
    dashboard: <><rect x="3" y="3" width="7" height="9" rx="1.5" {...p} /><rect x="14" y="3" width="7" height="5" rx="1.5" {...p} /><rect x="14" y="12" width="7" height="9" rx="1.5" {...p} /><rect x="3" y="16" width="7" height="5" rx="1.5" {...p} /></>,
    book: <><path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H19a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H5.5A1.5 1.5 0 0 0 4 21.5z" {...p} /><path d="M4 17.5A1.5 1.5 0 0 1 5.5 16H20" {...p} /></>,
    bulb: <><path d="M9 18h6M10 21h4" {...p} /><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.3 1 2.1h5c0-.8.4-1.6 1-2.1A6 6 0 0 0 12 3z" {...p} /></>,
    scale: <><path d="M12 3v18M7 21h10" {...p} /><path d="M5 7h14M5 7l-2.5 5a2.5 2.5 0 0 0 5 0zM19 7l-2.5 5a2.5 2.5 0 0 0 5 0z" {...p} /><path d="M5 7l7-2 7 2" {...p} /></>,
    flask: <><path d="M9 3h6M10 3v6l-4.5 8A2 2 0 0 0 7.3 20h9.4a2 2 0 0 0 1.8-3L14 9V3" {...p} /><path d="M7.5 15h9" {...p} /></>,
    pen: <><path d="M12 20h9" {...p} /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" {...p} /></>,
    shield: <><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z" {...p} /><path d="M9 12l2 2 4-4" {...p} /></>,
    gate: <><path d="M6 21V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v15" {...p} /><path d="M3 21h18M9 21V11h6v10M9 8h6" {...p} /></>,
    bell: <><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" {...p} /><path d="M13.7 21a2 2 0 0 1-3.4 0" {...p} /></>,
    search: <><circle cx="11" cy="11" r="7" {...p} /><path d="M21 21l-4-4" {...p} /></>,
    settings: <><circle cx="12" cy="12" r="3" {...p} /><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 2.6 14H2a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4 7a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 9 2.6V2a2 2 0 1 1 4 0v.1A1.6 1.6 0 0 0 17 4a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.6 1.6 0 0 0 21.4 9H21a2 2 0 1 1 0 4z" {...p} /></>,
    plus: <><path d="M12 5v14M5 12h14" {...p} /></>,
    chevron: <><path d="M9 6l6 6-6 6" {...p} /></>,
    chevDown: <><path d="M6 9l6 6 6-6" {...p} /></>,
    arrow: <><path d="M5 12h14M13 6l6 6-6 6" {...p} /></>,
    link: <><path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1" {...p} /><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1" {...p} /></>,
    check: <><path d="M4 12l5 5L20 6" {...p} /></>,
    x: <><path d="M6 6l12 12M18 6L6 18" {...p} /></>,
    play: <><path d="M7 5l11 7-11 7z" {...p} /></>,
    pause: <><path d="M8 5v14M16 5v14" {...p} /></>,
    clock: <><circle cx="12" cy="12" r="9" {...p} /><path d="M12 7v5l3 2" {...p} /></>,
    cpu: <><rect x="6" y="6" width="12" height="12" rx="2" {...p} /><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" {...p} /><rect x="10" y="10" width="4" height="4" rx="1" {...p} /></>,
    server: <><rect x="3" y="4" width="18" height="7" rx="2" {...p} /><rect x="3" y="13" width="18" height="7" rx="2" {...p} /><path d="M7 7.5h.01M7 16.5h.01" {...p} /></>,
    file: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" {...p} /><path d="M14 3v5h5" {...p} /></>,
    git: <><circle cx="6" cy="6" r="2.5" {...p} /><circle cx="6" cy="18" r="2.5" {...p} /><circle cx="18" cy="9" r="2.5" {...p} /><path d="M6 8.5v7M18 11.5c0 3-3 3.5-6 3.5" {...p} /></>,
    chart: <><path d="M4 20V4M4 20h16" {...p} /><path d="M7 16l4-5 3 3 4-7" {...p} /></>,
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" {...p} /><rect x="14" y="3" width="7" height="7" rx="1.5" {...p} /><rect x="3" y="14" width="7" height="7" rx="1.5" {...p} /><rect x="14" y="14" width="7" height="7" rx="1.5" {...p} /></>,
    layers: <><path d="M12 3l9 5-9 5-9-5z" {...p} /><path d="M3 13l9 5 9-5M3 17l9 5 9-5" {...p} opacity="0.5" /></>,
    sparkle: <><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" {...p} /></>,
    refresh: <><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5" {...p} /></>,
    dot: <circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: "block", ...style }}>{paths[name] || null}</svg>;
}

// status label maps
const STATUS = {
  candidate:  { cls: "st-candidate",  zh: "候选",   en: "candidate" },
  accepted:   { cls: "st-accepted",   zh: "已采纳", en: "accepted" },
  implemented:{ cls: "st-implemented",zh: "已实验", en: "implemented" },
  "paper.drafted": { cls: "st-drafted", zh: "初稿", en: "drafted" },
  "paper.reviewed":{ cls: "st-reviewed", zh: "已评审", en: "reviewed" },
  drafted:    { cls: "st-drafted",    zh: "初稿",   en: "drafted" },
  reviewed:   { cls: "st-reviewed",   zh: "已评审", en: "reviewed" },
  rejected:   { cls: "st-rejected",   zh: "已淘汰", en: "rejected" },
  running:    { cls: "st-running",    zh: "运行中", en: "running" },
  done:       { cls: "st-implemented",zh: "完成",   en: "done" },
  summarized: { cls: "st-implemented",zh: "已综合", en: "summarized" },
  ingested:   { cls: "st-drafted",    zh: "已收录", en: "ingested" },
  planned:    { cls: "st-candidate",  zh: "已规划", en: "planned" },
  pending:    { cls: "st-candidate",  zh: "待审批", en: "pending" },
  approved:   { cls: "st-implemented",zh: "已批准", en: "approved" },
};

function StatusPill({ status, sm }) {
  const s = STATUS[status] || { cls: "", zh: status, en: status };
  return <span className={"pill " + s.cls + (sm ? " sm" : "")}><span className="dot" />{s.zh}<span style={{ opacity: .55, fontWeight: 500, fontSize: "0.9em" }}>{s.en}</span></span>;
}

// Score ring (conic gradient)
function ScoreRing({ value, max = 10, size = 46, label }) {
  const pct = Math.max(0, Math.min(1, value / max));
  const deg = pct * 360;
  const color = value >= 7.5 ? "var(--ok)" : value >= 6 ? "var(--accent)" : "var(--warn)";
  return (
    <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
      <div style={{ width: size, height: size, borderRadius: "50%", background: `conic-gradient(${color} ${deg}deg, var(--surface-3) ${deg}deg)` }} />
      <div style={{ position: "absolute", inset: 4, borderRadius: "50%", background: "var(--surface)", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: size * 0.3, fontWeight: 700, color: "var(--text)" }}>{value.toFixed(1)}</span>
        {label && <span style={{ fontSize: 8, color: "var(--text-3)", marginTop: -1 }}>{label}</span>}
      </div>
    </div>
  );
}

// Bilingual title helper
function BiTitle({ zh, en, size = 15 }) {
  return (
    <div>
      <div style={{ fontSize: size, fontWeight: 650, color: "var(--text)", letterSpacing: "-0.01em", lineHeight: 1.3 }}>{zh}</div>
      {en && <div style={{ fontSize: size - 2.5, color: "var(--text-3)", marginTop: 2, lineHeight: 1.3 }}>{en}</div>}
    </div>
  );
}

// Eyebrow + title page header
function PageHead({ eyebrow, title, sub, en, right }) {
  return (
    <div className="row" style={{ alignItems: "flex-start", marginBottom: 26 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="h-eyebrow">{eyebrow}</div>
        <h1 className="h-title">{title}</h1>
        {sub && <p className="h-sub">{sub}{en && <span className="en"> · {en}</span>}</p>}
      </div>
      {right && <div className="row gap10" style={{ flexShrink: 0, marginLeft: 16 }}>{right}</div>}
    </div>
  );
}

// Wikilink chip
function Wiki({ children, onClick }) {
  return <span className="wikilink" onClick={onClick}>[[{children}]]</span>;
}

// little metric delta
function Delta({ children, down }) {
  return <span className={down ? "delta-down" : "delta-up"}>{!down && "▲ "}{down && "▼ "}{children}</span>;
}

// Pipeline stage chip used in nav + dashboard
function StageDot({ active, done, color = "var(--accent)" }) {
  return <span style={{ width: 9, height: 9, borderRadius: "50%", background: done || active ? color : "var(--surface-3)", boxShadow: active ? `0 0 0 3px var(--accent-soft)` : "none", flexShrink: 0 }} />;
}

// Segmented control
function Segmented({ options, value, onChange }) {
  return (
    <div style={{ display: "inline-flex", background: "var(--surface-2)", border: "0.5px solid var(--border-2)", borderRadius: 9, padding: 3, gap: 2 }}>
      {options.map(o => {
        const v = typeof o === "string" ? o : o.v;
        const lab = typeof o === "string" ? o : o.label;
        const on = v === value;
        return (
          <button key={v} onClick={() => onChange(v)} style={{
            border: "none", cursor: "pointer", borderRadius: 6, padding: "5px 13px", fontSize: 12.5, fontWeight: 600,
            fontFamily: "var(--sans)", background: on ? "var(--surface)" : "transparent",
            color: on ? "var(--text)" : "var(--text-3)", boxShadow: on ? "var(--shadow-card)" : "none", transition: "all .12s",
          }}>{lab}</button>
        );
      })}
    </div>
  );
}

Object.assign(window, { UI: { Icon, StatusPill, ScoreRing, BiTitle, PageHead, Wiki, Delta, StageDot, Segmented, STATUS } });
window.Icon = Icon;
