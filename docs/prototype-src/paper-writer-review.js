/* ============================================================
   Paper Writer + Paper Review
   ============================================================ */
const { Icon: I6, StatusPill: SP6, Wiki: W6, Segmented: SEG6 } = window.UI;

/* ---------------- Paper preview (mini LaTeX render) ---------------- */
function PaperPreview({ paper }) {
  return (
    <div style={{ background: "#fff", border: "0.5px solid var(--border-2)", borderRadius: 6, boxShadow: "var(--shadow-card)", padding: "34px 38px", fontFamily: '"Times New Roman", Georgia, serif', color: "#1a1a1a", lineHeight: 1.5 }}>
      <div style={{ textAlign: "center", fontSize: 16, fontWeight: 700, lineHeight: 1.3, marginBottom: 10 }}>{paper.title}</div>
      <div style={{ textAlign: "center", fontSize: 11, color: "#888", marginBottom: 18, fontStyle: "italic" }}>Anonymous Author(s) · Paper under double-blind review</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, fontSize: 9.5, textAlign: "justify", color: "#333" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 10, marginBottom: 4 }}>Abstract</div>
          <p style={{ margin: "0 0 8px" }}>Autonomous idea-generation agents are prone to <i>novelty hallucination</i>: proposals that appear novel under embedding similarity yet duplicate prior work in the citation graph. We introduce a reranking step that re-estimates the distance between each idea and its nearest prior using a 2-hop co-citation subgraph with citation time-decay and degree normalization.</p>
          <p style={{ margin: 0 }}>On a held-out evaluation set, our method raises novelty-detection precision from 0.736 to 0.830 (+9.4pt) without sacrificing recall, and ablations isolate the contribution of each component.</p>
          <div style={{ fontWeight: 700, fontSize: 10, margin: "10px 0 4px" }}>1&nbsp;&nbsp;Introduction</div>
          <p style={{ margin: 0 }}>Recent automated-science systems generate research ideas at scale, but human studies show that feasibility and novelty are systematically over-estimated…</p>
        </div>
        <div>
          <div style={{ fontWeight: 700, fontSize: 10, marginBottom: 4 }}>3&nbsp;&nbsp;Method</div>
          <p style={{ margin: "0 0 6px" }}>Given a candidate idea <i>i</i> and a literature graph <i>G</i>, we retrieve the k-nearest priors and expand a 2-hop co-citation neighborhood…</p>
          <div style={{ height: 52, background: "repeating-linear-gradient(45deg,#f3f1ee,#f3f1ee 6px,#ebe8e3 6px,#ebe8e3 12px)", borderRadius: 3, display: "flex", alignItems: "center", justifyContent: "center", margin: "8px 0", border: "0.5px solid #e0ddd6" }}>
            <span style={{ fontFamily: "var(--mono)", fontSize: 8, color: "#999" }}>Figure 1 · figures/curve.pdf</span>
          </div>
          <p style={{ margin: 0, fontSize: 9.5 }}>Table 1 reports precision/recall across ablations, confirming each component contributes…</p>
        </div>
      </div>
    </div>
  );
}

function PaperWriter({ openGates }) {
  const D = window.DATA, p = D.paper;
  const totalWords = p.sections.reduce((s, x) => s + x.words, 0);
  return (
    <div className="page fadeup">
      <div className="row" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div style={{ flex: 1 }}>
          <div className="h-eyebrow">Skill 5 · paper-writer</div>
          <h1 className="h-title" style={{ fontSize: 22 }}>论文撰写 <span style={{ fontSize: 14, color: "var(--text-3)", fontWeight: 400 }}>Paper Writer</span></h1>
          <div className="row gap10" style={{ marginTop: 10 }}>
            <span className="pill" style={{ background: "var(--info-bg)", color: "var(--info-tx)" }}>{p.venue}</span>
            <span className="pill"><I6 name="file" size={11} />{p.pages}/{p.pageLimit} 页</span>
            <span className="pill">anonymized ✓</span>
            <span className="mono muted" style={{ fontSize: 11 }}>data/papers/{p.idea}</span>
          </div>
        </div>
        <div className="row gap10">
          <button className="btn btn-ghost"><I6 name="refresh" size={14} />build</button>
          <button className="btn btn-primary" onClick={() => openGates("G-20260529-004")}><I6 name="arrow" size={14} />投稿 · 人闸</button>
        </div>
      </div>

      <div className="row gap20" style={{ alignItems: "flex-start" }}>
        {/* left: preview */}
        <div style={{ flex: 1.5, minWidth: 0 }}>
          <PaperPreview paper={p} />
          <div className="card" style={{ marginTop: 18, overflow: "hidden" }}>
            <div className="card-pad row" style={{ justifyContent: "space-between", paddingBottom: 12 }}>
              <span className="section-h"><I6 name="link" size={15} style={{ color: "var(--accent)" }} />refs.bib · Semantic Scholar 校验</span>
              <span className="pill sm" style={{ background: "var(--ok-bg)", color: "var(--ok-tx)" }}>{p.bib.length}/{p.bib.length} 真实存在</span>
            </div>
            <table className="tbl">
              <thead><tr><th>cite key</th><th>arXiv</th><th>标题</th><th>SS 校验</th></tr></thead>
              <tbody>
                {p.bib.map((b, i) => (
                  <tr key={i}>
                    <td className="mono" style={{ fontSize: 11.5 }}>{b.key}</td>
                    <td className="mono muted" style={{ fontSize: 11.5 }}>{b.arxiv || "—"}</td>
                    <td style={{ fontSize: 12 }}>{b.title}</td>
                    <td><span style={{ color: "var(--ok-tx)" }}><I6 name="check" size={14} style={{ display: "inline-block", verticalAlign: "-2px" }} /></span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* right: sections + build */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="card" style={{ overflow: "hidden", marginBottom: 18 }}>
            <div className="card-pad" style={{ paddingBottom: 8 }}><span className="section-h"><I6 name="pen" size={15} style={{ color: "var(--accent)" }} />分节进度 <span className="en-label" style={{ fontSize: 11 }}>{totalWords} words</span></span></div>
            <div style={{ padding: "0 8px 10px" }}>
              {p.sections.map((s, i) => (
                <div key={i} className="row gap10" style={{ padding: "10px 14px", borderTop: i ? "0.5px solid var(--border)" : "none" }}>
                  <div style={{ width: 18, height: 18, borderRadius: "50%", flexShrink: 0, background: s.status === "done" ? "var(--ok-bg)" : "var(--warn-bg)", color: s.status === "done" ? "var(--ok-tx)" : "var(--warn-tx)", display: "flex", alignItems: "center", justifyContent: "center" }}>{s.status === "done" ? <I6 name="check" size={11} /> : <I6 name="pen" size={10} />}</div>
                  <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>{s.name}</span>
                  <span className="mono muted" style={{ fontSize: 11 }}>{s.words}w</span>
                </div>
              ))}
            </div>
          </div>

          <div className="card card-pad" style={{ marginBottom: 18 }}>
            <span className="section-h" style={{ marginBottom: 12 }}><I6 name="check" size={14} style={{ color: "var(--accent)" }} />latex_build</span>
            <div className="codeblock" style={{ fontSize: 11.5 }}>{`$ latexmk -pdf main.tex\n`}<span className="s">{p.build.log}</span>{`\n`}<span className="c">overfull boxes: {p.build.overfull}</span>{`\n`}<span className="c">undefined refs: {p.build.undefinedRefs}</span>{`\n`}<span className="c">missing figures: {p.build.missingFigs}</span></div>
          </div>

          <div className="card card-pad" style={{ background: "var(--surface-2)" }}>
            <span className="section-h" style={{ marginBottom: 10 }}><I6 name="shield" size={14} style={{ color: "var(--accent)" }} />写时约束</span>
            {["实验数字只能来自 results.md，禁止编造或外推", "每个 \\cite 必须在 refs.bib 有真实条目", "遵守 venue 匿名规则与章节惯例", "writer ≠ reviewer（论文层面禁止自评）"].map((t, i) => (
              <div key={i} className="row gap8" style={{ fontSize: 12, color: "var(--text-2)", marginBottom: 7, alignItems: "flex-start" }}><span style={{ marginTop: 5, width: 5, height: 5, borderRadius: "50%", background: "var(--accent)", flexShrink: 0 }} />{t}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------- Paper Review ---------------- */
function CiteVerifyRow({ c }) {
  const bad = c.exist === "fail" || c.correct === "fail";
  const warn = c.correct === "warn";
  const color = bad ? "var(--danger)" : warn ? "var(--warn)" : "var(--ok)";
  const bg = bad ? "var(--danger-bg)" : warn ? "var(--warn-bg)" : "transparent";
  return (
    <div style={{ padding: "13px 16px", borderBottom: "0.5px solid var(--border)", background: bg }}>
      <div className="row gap10">
        <span className="mono" style={{ fontSize: 11.5, fontWeight: 600, color: bad ? "var(--danger-tx)" : "var(--text)" }}>\cite{`{${c.key}}`}</span>
        <span className="mono muted" style={{ fontSize: 10.5 }}>L{c.line}</span>
        <div className="row gap6" style={{ marginLeft: "auto" }}>
          <span className="pill sm" style={{ background: c.exist === "ok" ? "var(--ok-bg)" : "var(--danger-bg)", color: c.exist === "ok" ? "var(--ok-tx)" : "var(--danger-tx)" }}>{c.exist === "ok" ? "存在 ✓" : "虚构 ✗"}</span>
          <span className="pill sm" style={{ background: c.correct === "ok" ? "var(--ok-bg)" : c.correct === "warn" ? "var(--warn-bg)" : "var(--danger-bg)", color: c.correct === "ok" ? "var(--ok-tx)" : c.correct === "warn" ? "var(--warn-tx)" : "var(--danger-tx)" }}>{c.correct === "ok" ? "论述一致 ✓" : c.correct === "warn" ? "断章取义 ⚠" : "张冠李戴 ✗"}</span>
        </div>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 6, lineHeight: 1.5, paddingLeft: 2 }}>{c.note}</div>
    </div>
  );
}

function PaperReview() {
  const D = window.DATA, r = D.review;
  const [tab, setTab] = React.useState("form");
  const issues = r.form.cites.filter(c => c.exist !== "ok" || c.correct !== "ok").length;
  return (
    <div className="page fadeup">
      <div className="row" style={{ alignItems: "flex-start", marginBottom: 20 }}>
        <div style={{ flex: 1 }}>
          <div className="h-eyebrow">Skill 6 · paper-review</div>
          <h1 className="h-title" style={{ fontSize: 22 }}>论文评审 <span style={{ fontSize: 14, color: "var(--text-3)", fontWeight: 400 }}>Paper Review</span></h1>
          <p className="h-sub" style={{ marginTop: 8 }}>两阶段：先形式 + 引用核验（防幻觉），再顶会评审视角。reviewer 独立于 writer，禁止自评。</p>
        </div>
        <SEG6 options={[{ v: "form", label: "形式 + 引用核验" }, { v: "peer", label: "顶会评审" }]} value={tab} onChange={setTab} />
      </div>

      {tab === "form" ? (
        <div>
          <div className="row gap16" style={{ marginBottom: 20 }}>
            <div className="card card-pad" style={{ flex: 1 }}><div className="row gap8" style={{ marginBottom: 6 }}><I6 name="check" size={15} style={{ color: "var(--ok-tx)" }} /><span style={{ fontSize: 12.5, fontWeight: 600 }}>语言</span></div><div style={{ fontSize: 12, color: "var(--text-2)" }}>{r.form.lang[0].text}</div></div>
            <div className="card card-pad" style={{ flex: 1 }}><div className="row gap8" style={{ marginBottom: 6 }}><I6 name="check" size={15} style={{ color: "var(--ok-tx)" }} /><span style={{ fontSize: 12.5, fontWeight: 600 }}>格式</span></div><div style={{ fontSize: 12, color: "var(--text-2)" }}>{r.form.format[0].text}</div></div>
            <div className="card card-pad" style={{ flex: 1, background: "var(--danger-bg)", borderColor: "var(--danger)" }}><div className="row gap8" style={{ marginBottom: 6 }}><I6 name="shield" size={15} style={{ color: "var(--danger-tx)" }} /><span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--danger-tx)" }}>引用核验</span></div><div style={{ fontSize: 12, color: "var(--danger-tx)" }}><b>{issues} 处问题</b>：1 虚构 + 1 断章取义</div></div>
          </div>

          <div className="card" style={{ overflow: "hidden" }}>
            <div className="card-pad" style={{ paddingBottom: 12 }}>
              <span className="section-h"><I6 name="shield" size={15} style={{ color: "var(--accent)" }} />cite_verify · 逐条引用核验 <span className="en-label" style={{ fontSize: 11 }}>存在性 + 论述一致性</span></span>
            </div>
            {r.form.cites.map((c, i) => <CiteVerifyRow key={i} c={c} />)}
          </div>
          <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 12, lineHeight: 1.5 }}>存在性：Semantic Scholar 查标题/作者/年份匹配，抓虚构引用。正确性：读 vault papers 页或原文 GROBID，比对引用处论述与被引论文实际内容，抓张冠李戴 / 断章取义。</div>
        </div>
      ) : (
        <div>
          <div className="card card-pad" style={{ marginBottom: 20, background: "linear-gradient(135deg, var(--surface), var(--violet-bg) 260%)" }}>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <div><div style={{ fontSize: 12, color: "var(--text-3)" }}>Meta-review · {r.venue}</div><div className="row gap10" style={{ alignItems: "baseline", marginTop: 4 }}><span className="mono" style={{ fontSize: 30, fontWeight: 700 }}>{r.peer.meta.rating}</span><span style={{ fontSize: 13, color: "var(--text-2)" }}>/ 10 平均评分</span></div></div>
              <span className="pill" style={{ background: "var(--violet-bg)", color: "var(--violet-tx)", fontSize: 13, height: 28 }}>{r.peer.meta.recommend}</span>
            </div>
          </div>

          <div className="card" style={{ overflow: "hidden", marginBottom: 20 }}>
            <div className="card-pad" style={{ paddingBottom: 8 }}><span className="section-h"><I6 name="scale" size={15} style={{ color: "var(--accent)" }} />多审集成 <span className="en-label" style={{ fontSize: 11 }}>N=3 不同视角</span></span></div>
            <table className="tbl">
              <thead><tr><th>reviewer</th><th>soundness</th><th>presentation</th><th>contribution</th><th>rating</th><th>confidence</th></tr></thead>
              <tbody>
                {r.peer.reviewers.map((rv, i) => (
                  <tr key={i}>
                    <td className="mono" style={{ fontWeight: 600 }}>{rv.id}</td>
                    <td className="mono">{rv.soundness}/4</td><td className="mono">{rv.presentation}/4</td><td className="mono">{rv.contribution}/4</td>
                    <td><span className="mono" style={{ fontWeight: 700, color: "var(--accent-text)" }}>{rv.rating}</span>/10</td>
                    <td className="mono muted">{rv.conf}/5</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="row gap16" style={{ alignItems: "flex-start" }}>
            <div className="card card-pad" style={{ flex: 1 }}>
              <span className="section-h" style={{ marginBottom: 12, color: "var(--ok-tx)" }}><I6 name="check" size={14} />Strengths</span>
              {r.peer.strengths.map((s, i) => <div key={i} className="row gap8" style={{ fontSize: 12.5, marginBottom: 10, lineHeight: 1.5, alignItems: "flex-start" }}><span style={{ marginTop: 5, width: 5, height: 5, borderRadius: "50%", background: "var(--ok)", flexShrink: 0 }} />{s}</div>)}
            </div>
            <div className="card card-pad" style={{ flex: 1 }}>
              <span className="section-h" style={{ marginBottom: 12, color: "var(--danger-tx)" }}><I6 name="x" size={14} />Weaknesses</span>
              {r.peer.weaknesses.map((s, i) => <div key={i} className="row gap8" style={{ fontSize: 12.5, marginBottom: 10, lineHeight: 1.5, alignItems: "flex-start" }}><span style={{ marginTop: 5, width: 5, height: 5, borderRadius: "50%", background: "var(--danger)", flexShrink: 0 }} />{s}</div>)}
            </div>
          </div>

          <div className="card card-pad" style={{ marginTop: 16, background: "var(--surface-2)" }}>
            <span className="section-h" style={{ marginBottom: 12 }}><I6 name="bell" size={14} style={{ color: "var(--accent)" }} />Questions to authors</span>
            {r.peer.questions.map((q, i) => <div key={i} className="row gap10" style={{ fontSize: 12.5, marginBottom: 9, alignItems: "flex-start" }}><span className="mono" style={{ color: "var(--accent-text)", flexShrink: 0 }}>Q{i + 1}</span><span style={{ lineHeight: 1.5 }}>{q}</span></div>)}
          </div>
        </div>
      )}
    </div>
  );
}

window.PaperWriter = PaperWriter;
window.PaperReview = PaperReview;
