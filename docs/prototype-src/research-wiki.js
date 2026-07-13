/* ============================================================
   Research Wiki — multi-direction vaults · papers · concept mgmt
   ============================================================ */
const { Icon: I2, StatusPill: SP2, Wiki: W2, Segmented: SEG2 } = window.UI;

const CONCEPT_KIND = {
  method: { zh: "方法", c: "var(--accent-text)", bg: "var(--accent-soft)" },
  architecture: { zh: "架构", c: "var(--info-tx)", bg: "var(--info-bg)" },
  methodology: { zh: "方法论", c: "var(--violet-tx)", bg: "var(--violet-bg)" },
  problem: { zh: "问题", c: "var(--danger-tx)", bg: "var(--danger-bg)" },
  metric: { zh: "指标", c: "var(--ok-tx)", bg: "var(--ok-bg)" },
  dataset: { zh: "数据集", c: "var(--warn-tx)", bg: "var(--warn-bg)" },
};
const DIR_STATUS = {
  active: { zh: "active", c: "var(--ok-tx)", bg: "var(--ok-bg)", dot: "var(--ok)" },
  paused: { zh: "paused", c: "var(--warn-tx)", bg: "var(--warn-bg)", dot: "var(--warn)" },
  archived: { zh: "archived", c: "var(--text-3)", bg: "var(--surface-3)", dot: "var(--muted-dot)" },
};

/* ---------------- direction switcher ---------------- */
function DirectionSwitcher({ dirs, current, onSwitch }) {
  const [open, setOpen] = React.useState(false);
  const cur = dirs.find(d => d.slug === current);
  const ref = React.useRef(null);
  React.useEffect(() => {
    function h(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
    document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h);
  }, []);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <div className="hoverable" onClick={() => setOpen(o => !o)} style={{ display: "flex", alignItems: "center", gap: 10, padding: "7px 12px 7px 10px", border: "0.5px solid var(--border-2)", borderRadius: 10, background: "var(--surface)", cursor: "pointer", minWidth: 280 }}>
        <div style={{ width: 30, height: 30, borderRadius: 8, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}><I2 name="book" size={16} /></div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, lineHeight: 1.2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{cur.title}</div>
          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-3)" }}>vaults/{cur.slug}</div>
        </div>
        <I2 name="chevDown" size={15} style={{ color: "var(--text-3)", transform: open ? "rotate(180deg)" : "none", transition: "transform .15s" }} />
      </div>
      {open && (
        <div style={{ position: "absolute", top: "calc(100% + 6px)", left: 0, width: 360, background: "var(--surface)", border: "0.5px solid var(--border-2)", borderRadius: 12, boxShadow: "var(--shadow-pop)", zIndex: 30, overflow: "hidden", padding: 6 }}>
          <div className="sb-section" style={{ padding: "8px 10px 6px" }}>研究方向 · {dirs.length} directions</div>
          {dirs.map(d => {
            const st = DIR_STATUS[d.status] || DIR_STATUS.active, on = d.slug === current;
            return (
              <div key={d.slug} className="hoverable" onClick={() => { onSwitch(d.slug); setOpen(false); }} style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 10px", borderRadius: 9, cursor: "pointer", background: on ? "var(--surface-2)" : "transparent" }}>
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: st.dot, flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="row gap8"><span style={{ fontSize: 13, fontWeight: 600 }}>{d.title}</span>{!d.initialized && <span className="pill sm" style={{ background: "var(--warn-bg)", color: "var(--warn-tx)" }}>未初始化</span>}</div>
                  <div className="mono" style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 2 }}>{d.initialized ? `${d.counts.papers} 论文 · ${d.counts.concepts} 概念` : "等待 init 冷启动"}</div>
                </div>
                {on && <I2 name="check" size={15} style={{ color: "var(--accent)", flexShrink: 0 }} />}
              </div>
            );
          })}
          <div className="hr" style={{ margin: "6px 4px" }} />
          <div className="hoverable" style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 10px", borderRadius: 9, cursor: "pointer", color: "var(--accent-text)" }}>
            <I2 name="plus" size={15} /><span style={{ fontSize: 13, fontWeight: 600 }}>新建研究方向</span><span className="mono" style={{ fontSize: 10.5, color: "var(--text-4)", marginLeft: "auto" }}>/research-wiki new</span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------- cold-start (uninitialized vault) ---------------- */
function ColdStart({ dir }) {
  const [running, setRunning] = React.useState(false);
  const [step, setStep] = React.useState(-1);
  const steps = ["arxiv_fetch 近 6 月候选", "bootstrap_seed 综述 + citation 雪球", "relevance_filter LLM 打分筛选", "pdf_extract 下载 PDF + PyMuPDF 抽全文", "librarian 读全文建 index / surveys / concepts"];
  function run() { setRunning(true); setStep(0); let s = 0; const t = () => { setStep(s); if (s < steps.length - 1) { s++; setTimeout(t, 700); } }; t(); }
  return (
    <div className="scroll" style={{ overflowY: "auto", flex: 1, padding: "40px" }}>
      <div style={{ maxWidth: 620, margin: "0 auto", textAlign: "center" }}>
        <div style={{ width: 56, height: 56, borderRadius: 15, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 18px" }}><I2 name="book" size={28} /></div>
        <h2 style={{ fontSize: 20, fontWeight: 680, margin: "0 0 8px" }}>该方向尚未初始化</h2>
        <p style={{ fontSize: 13.5, color: "var(--text-2)", lineHeight: 1.6, margin: "0 0 8px" }}>空 Wiki 直接日更会让 idea-forge「无米下锅」。冷启动 <span className="mono" style={{ color: "var(--accent-text)" }}>init</span> 用一次性回填近 {dir.bootstrap_months} 个月的研究 + 综述 + citation 雪球，建立知识地图。</p>
        <div className="mono" style={{ fontSize: 11, color: "var(--text-3)", marginBottom: 24 }}>research_direction.md 已就绪 · {dir.keywords.length} keywords · {dir.categories.join(" / ")}</div>

        <div className="card" style={{ textAlign: "left", overflow: "hidden", marginBottom: 22 }}>
          {steps.map((s, i) => {
            const done = running && step > i, cur = running && step === i;
            return (
              <div key={i} className="row gap12" style={{ padding: "12px 18px", borderBottom: i < steps.length - 1 ? "0.5px solid var(--border)" : "none", opacity: running && step < i ? 0.4 : 1 }}>
                <div style={{ width: 22, height: 22, borderRadius: "50%", flexShrink: 0, background: done ? "var(--ok-bg)" : cur ? "var(--accent-soft)" : "var(--surface-2)", color: done ? "var(--ok-tx)" : "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center" }} className={cur ? "pulse" : ""}>
                  {done ? <I2 name="check" size={12} /> : <span className="mono" style={{ fontSize: 11 }}>{i + 1}</span>}
                </div>
                <span style={{ fontSize: 13, fontWeight: 500 }}>{s}</span>
                {cur && <span className="mono" style={{ fontSize: 10.5, color: "var(--accent-text)", marginLeft: "auto" }}>running…</span>}
              </div>
            );
          })}
        </div>
        <div className="row gap10" style={{ justifyContent: "center" }}>
          <button className="btn btn-primary" onClick={run} disabled={running}>{running ? <><I2 name="refresh" size={14} className="spin" />init 运行中…</> : <><I2 name="play" size={14} />运行 init 冷启动</>}</button>
          <button className="btn btn-ghost"><I2 name="settings" size={14} />--max-papers 200</button>
        </div>
        <div style={{ fontSize: 11, color: "var(--text-4)", marginTop: 16 }}>支持 --resume 断点续跑；成本旋钮：--max-papers 与 relevance 阈值。</div>
      </div>
    </div>
  );
}

/* ---------------- shared ---------------- */
function RelevanceBar({ v }) {
  return (
    <div className="row gap8" style={{ width: 92 }}>
      <div className="bar" style={{ flex: 1 }}><i style={{ width: `${v * 100}%`, background: v >= 0.85 ? "var(--ok)" : v >= 0.7 ? "var(--accent)" : "var(--warn)" }} /></div>
      <span className="mono" style={{ fontSize: 10.5, color: "var(--text-3)" }}>{v.toFixed(2)}</span>
    </div>
  );
}
function PaperRow({ p, active, onClick }) {
  return (
    <div onClick={onClick} style={{ padding: "13px 18px", cursor: "pointer", borderBottom: "0.5px solid var(--border)", background: active ? "var(--surface)" : "transparent", borderLeft: active ? "2px solid var(--accent)" : "2px solid transparent" }}>
      <div className="row gap8" style={{ marginBottom: 5 }}>
        <span className="mono" style={{ fontSize: 10.5, color: active ? "var(--accent-text)" : "var(--text-3)" }}>{p.arxiv ? p.arxiv : "blog"}</span>
        {p.kind === "survey" && <span className="pill sm" style={{ background: "var(--surface-3)" }}>survey</span>}
        {p.featured && <I2 name="sparkle" size={12} style={{ color: "var(--accent)" }} />}
        <span style={{ marginLeft: "auto" }}><RelevanceBar v={p.relevance} /></span>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: "var(--text)", marginBottom: 6 }}>{p.title}</div>
      <div className="row gap6 wrap">{p.tags.slice(0, 3).map(t => <span key={t} className="tag">{t}</span>)}</div>
    </div>
  );
}
function ConceptRow({ c, active, onClick }) {
  const k = CONCEPT_KIND[c.kind] || CONCEPT_KIND.method;
  return (
    <div onClick={onClick} style={{ padding: "12px 18px", cursor: "pointer", borderBottom: "0.5px solid var(--border)", background: active ? "var(--surface)" : "transparent", borderLeft: active ? "2px solid var(--accent)" : "2px solid transparent" }}>
      <div className="row gap8" style={{ marginBottom: 4 }}>
        <span className="pill sm" style={{ background: k.bg, color: k.c }}>{k.zh}</span>
        <span style={{ marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-3)" }}>{c.n}×</span>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.3 }}>{c.title}</div>
      <div className="mono" style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 3 }}>{c.titleEn}</div>
    </div>
  );
}
function FrontmatterRow({ k, children }) {
  return (
    <div className="row" style={{ gap: 14, padding: "5px 0", alignItems: "flex-start" }}>
      <span className="mono" style={{ fontSize: 11.5, color: "var(--accent-text)", width: 96, flexShrink: 0 }}>{k}:</span>
      <span style={{ fontSize: 12.5, color: "var(--text-2)", flex: 1 }}>{children}</span>
    </div>
  );
}
function Section({ title, children }) {
  return (
    <div style={{ marginTop: 22 }}>
      <div className="row gap8" style={{ marginBottom: 10 }}><span style={{ width: 3, height: 13, borderRadius: 2, background: "var(--accent)" }} /><span style={{ fontSize: 12, fontWeight: 700, color: "var(--text)" }}>{title}</span></div>
      {children}
    </div>
  );
}

/* ---------------- paper detail ---------------- */
function PaperDetail({ p, goPaper }) {
  if (!p) return <div className="empty">从左侧选择一篇论文</div>;
  const D = window.DATA;
  return (
    <div className="scroll fadeup" key={p.slug} style={{ overflowY: "auto", height: "100%", padding: "28px 34px 60px" }}>
      <div className="row gap8" style={{ marginBottom: 8 }}>
        <span className="pill" style={{ background: "var(--accent-soft)", color: "var(--accent-text)" }}>{p.kind === "survey" ? "综述" : "论文"} · {p.categories ? p.categories[0] : "cs.LG"}</span>
        <SP2 status={p.status} sm />{p.v > 1 && <span className="tag mono">v{p.v}</span>}
      </div>
      <h1 style={{ fontSize: 21, fontWeight: 680, lineHeight: 1.25, margin: "4px 0 6px" }}>{p.title}</h1>
      <div style={{ fontSize: 12.5, color: "var(--text-3)" }}>{p.authors.join(" · ")}</div>
      <div className="card card-pad" style={{ margin: "18px 0", background: "var(--surface-2)" }}>
        <div className="row" style={{ marginBottom: 8 }}><span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>--- vaults/{p.dir}/wiki/papers/{p.slug}.md ---</span></div>
        <FrontmatterRow k="arxiv_id">{p.arxiv ? `"${p.arxiv}"` : "null (blog)"}</FrontmatterRow>
        <FrontmatterRow k="published">{p.published}</FrontmatterRow>
        <FrontmatterRow k="ingested">{p.ingested}</FrontmatterRow>
        <FrontmatterRow k="relevance">{p.relevance}</FrontmatterRow>
        <FrontmatterRow k="cites">{p.cites.length ? p.cites.map(c => { const t = D.papers.find(x => x.arxiv === c || x.slug === c); return <span key={c} className="wikilink" onClick={() => t && goPaper(t.slug)} style={{ marginRight: 6 }}>[[{t ? t.slug : c}]]</span>; }) : <span className="muted">[]</span>}</FrontmatterRow>
        <FrontmatterRow k="tags">{p.tags.map(t => <span key={t} className="tag" style={{ marginRight: 6 }}>{t}</span>)}</FrontmatterRow>
      </div>
      <Section title="TL;DR"><p style={{ fontSize: 13.5, lineHeight: 1.6, margin: 0 }}>{p.tldr_zh}</p><p style={{ fontSize: 12.5, lineHeight: 1.55, color: "var(--text-3)", margin: "6px 0 0" }}>{p.tldr_en}</p></Section>
      <Section title="方法 · 核心贡献"><ul style={{ margin: 0, paddingLeft: 18 }}>{p.method.map((m, i) => <li key={i} style={{ fontSize: 13, lineHeight: 1.7 }}>{m}</li>)}</ul></Section>
      <Section title="与本研究方向的关系"><p style={{ fontSize: 13, lineHeight: 1.6, margin: 0 }}>{p.relation}</p></Section>
      <Section title="可借鉴点 · actionable">{p.take.map((t, i) => <div key={i} className="row gap10" style={{ marginBottom: 8, alignItems: "flex-start" }}><span style={{ marginTop: 5, width: 5, height: 5, borderRadius: "50%", background: "var(--accent)", flexShrink: 0 }} /><span style={{ fontSize: 13, lineHeight: 1.55 }}>{t}</span></div>)}</Section>
    </div>
  );
}

/* ---------------- concept detail ---------------- */
function ConceptDetail({ c, goPaper, goConcept }) {
  if (!c) return <div className="empty">从左侧选择一个概念</div>;
  const D = window.DATA;
  const k = CONCEPT_KIND[c.kind] || CONCEPT_KIND.method;
  const related = c.related.map(s => D.concepts.find(x => x.slug === s)).filter(Boolean);
  const refPapers = c.refs.map(s => D.papers.find(x => x.slug === s)).filter(Boolean);
  const ideas = (c.usedInIdeas || []).map(id => D.ideas.find(x => x.id === id)).filter(Boolean);
  return (
    <div className="scroll fadeup" key={c.slug} style={{ overflowY: "auto", height: "100%", padding: "28px 34px 60px" }}>
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <span className="pill" style={{ background: k.bg, color: k.c }}><span className="dot" />{k.zh} concept</span>
        <span className="mono muted" style={{ fontSize: 11 }}>{c.n} 处引用</span>
        {c.crossDir && <span className="pill sm" style={{ background: "var(--violet-bg)", color: "var(--violet-tx)" }}><I2 name="link" size={11} />cross-direction</span>}
      </div>
      <h1 style={{ fontSize: 23, fontWeight: 680, lineHeight: 1.2, margin: "2px 0 4px", letterSpacing: "-0.01em" }}>{c.title}</h1>
      <div className="mono" style={{ fontSize: 13, color: "var(--text-3)" }}>{c.titleEn}</div>

      <div className="card card-pad" style={{ margin: "20px 0", background: "var(--surface-2)" }}>
        <div className="row gap8" style={{ marginBottom: 8 }}><I2 name="sparkle" size={14} style={{ color: "var(--accent)" }} /><span style={{ fontSize: 12, fontWeight: 700 }}>定义 Definition</span></div>
        <p style={{ fontSize: 14, lineHeight: 1.65, margin: 0, color: "var(--text)" }}>{c.def}</p>
      </div>

      <Section title="出现于论文 · grounded in">
        {refPapers.length ? <div className="col gap8">{refPapers.map(p => (
          <div key={p.slug} className="card hoverable" onClick={() => goPaper(p.slug)} style={{ padding: "11px 14px" }}>
            <div className="row gap8"><span className="mono" style={{ fontSize: 10.5, color: "var(--accent-text)" }}>{p.arxiv || "blog"}</span><span style={{ marginLeft: "auto" }}><I2 name="arrow" size={13} style={{ color: "var(--text-4)" }} /></span></div>
            <div style={{ fontSize: 12.5, fontWeight: 600, marginTop: 4, lineHeight: 1.35 }}>{p.title}</div>
          </div>
        ))}</div> : <span className="muted" style={{ fontSize: 12.5 }}>暂无</span>}
      </Section>

      {related.length > 0 && (
        <Section title="相关概念 · related">
          <div className="row gap8 wrap">{related.map(r => { const rk = CONCEPT_KIND[r.kind] || CONCEPT_KIND.method; return <span key={r.slug} className="wikilink" onClick={() => goConcept(r.slug)} style={{ background: rk.bg, color: rk.c }}>[[{r.slug}]]</span>; })}</div>
        </Section>
      )}

      {ideas.length > 0 && (
        <Section title="被 idea 使用 · used in ideas">
          <div className="col gap8">{ideas.map(idea => (
            <div key={idea.id} className="card" style={{ padding: "10px 14px" }}>
              <div className="row gap8"><span className="mono" style={{ fontSize: 10.5, color: "var(--text-3)" }}>{idea.id}</span><SP2 status={idea.status} sm /></div>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginTop: 4 }}>{idea.title}</div>
            </div>
          ))}</div>
        </Section>
      )}

      <div style={{ marginTop: 24, fontSize: 11, color: "var(--text-4)", fontFamily: "var(--mono)" }}>vaults/{c.dir}/wiki/concepts/{c.slug}.md</div>
    </div>
  );
}

/* ---------------- main ---------------- */
function ResearchWiki({ currentDir, setCurrentDir }) {
  const D = window.DATA;
  const dir = D.directions.find(d => d.slug === currentDir) || D.directions[0];
  const [tab, setTab] = React.useState("papers");
  const dirPapers = D.papers.filter(p => p.dir === dir.slug);
  const dirConcepts = D.concepts.filter(c => c.dir === dir.slug);
  const [selPaper, setSelPaper] = React.useState(dirPapers[0] ? dirPapers[0].slug : null);
  const [selConcept, setSelConcept] = React.useState(dirConcepts[0] ? dirConcepts[0].slug : null);

  // reset selection when direction changes
  React.useEffect(() => {
    const ps = D.papers.filter(p => p.dir === dir.slug); const cs = D.concepts.filter(c => c.dir === dir.slug);
    setSelPaper(ps[0] ? ps[0].slug : null); setSelConcept(cs[0] ? cs[0].slug : null); setTab("papers");
  }, [currentDir]);

  function goPaper(slug) { setTab("papers"); setSelPaper(slug); }
  function goConcept(slug) { setTab("concepts"); setSelConcept(slug); }

  const selP = dirPapers.find(p => p.slug === selPaper) || dirPapers[0];
  const selC = dirConcepts.find(c => c.slug === selConcept) || dirConcepts[0];

  return (
    <div className="page-wide" style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* header */}
      <div style={{ padding: "18px 28px 14px", borderBottom: "0.5px solid var(--border)", background: "var(--app-bg)" }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div className="h-eyebrow">Skill 0 · research-wiki</div>
            <div className="row gap12" style={{ marginTop: 8 }}>
              <DirectionSwitcher dirs={D.directions} current={dir.slug} onSwitch={setCurrentDir} />
              <span className="pill" style={{ background: (DIR_STATUS[dir.status] || DIR_STATUS.active).bg, color: (DIR_STATUS[dir.status] || DIR_STATUS.active).c }}><span className="dot" />{dir.status}</span>
            </div>
          </div>
          <div className="row gap10">
            <button className="btn btn-ghost"><I2 name="search" size={14} />query</button>
            {dir.initialized
              ? <button className="btn btn-primary"><I2 name="refresh" size={14} />ingest 日更</button>
              : <button className="btn btn-primary"><I2 name="play" size={14} />init 冷启动</button>}
          </div>
        </div>
        {dir.initialized && (
          <div className="row gap20" style={{ marginTop: 16 }}>
            {[["papers", "论文", dir.counts.papers], ["surveys", "综述", dir.counts.surveys], ["concepts", "概念", dir.counts.concepts], ["ideas", "Idea", dir.counts.ideas]].map(([k, zh, n]) => (
              <div key={k} className="row gap6"><span className="mono" style={{ fontSize: 18, fontWeight: 700 }}>{n}</span><span style={{ fontSize: 12, color: "var(--text-3)" }}>{zh}</span></div>
            ))}
            <div className="row gap6" style={{ marginLeft: "auto" }}><I2 name="clock" size={13} style={{ color: "var(--text-3)" }} /><span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>last_ingest {dir.last_ingest}</span></div>
          </div>
        )}
      </div>

      {/* body */}
      {!dir.initialized ? <ColdStart dir={dir} /> : (
        <div className="split" style={{ flex: 1, minHeight: 0 }}>
          <div className="split-list">
            <div style={{ padding: "12px 16px", borderBottom: "0.5px solid var(--border)" }}>
              <SEG2 options={[{ v: "papers", label: `论文 ${dirPapers.length}` }, { v: "concepts", label: `概念 ${dirConcepts.length}` }]} value={tab} onChange={setTab} />
            </div>
            <div className="scroll" style={{ overflowY: "auto", flex: 1 }}>
              {tab === "concepts"
                ? dirConcepts.map(c => <ConceptRow key={c.slug} c={c} active={c.slug === (selC && selC.slug)} onClick={() => setSelConcept(c.slug)} />)
                : dirPapers.map(p => <PaperRow key={p.slug} p={p} active={p.slug === (selP && selP.slug)} onClick={() => setSelPaper(p.slug)} />)}
            </div>
          </div>
          <div className="split-detail">
            {tab === "concepts" ? <ConceptDetail c={selC} goPaper={goPaper} goConcept={goConcept} /> : <PaperDetail p={selP} goPaper={goPaper} />}
          </div>
        </div>
      )}
    </div>
  );
}

window.ResearchWiki = ResearchWiki;
