/* ============================================================
   Experiment Lab — plan · setup · run (Karpathy loop) · report
   ============================================================ */
const { Icon: I5, StatusPill: SP5, PageHead: PH5, Segmented: SEG5, Delta: D5, Wiki: W5 } = window.UI;

function MetricChart({ runs, baseline, target }) {
  const W = 560, H = 200, pad = { l: 44, r: 16, t: 16, b: 28 };
  const vals = runs.map(r => r.metric);
  const lo = Math.min(baseline, ...vals) - 0.02, hi = Math.max(target, ...vals) + 0.01;
  const x = i => pad.l + (i / (runs.length - 1)) * (W - pad.l - pad.r);
  const y = v => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);
  const kept = runs.filter(r => r.verdict !== "discard");
  const linePts = runs.map((r, i) => `${x(i)},${y(r.metric)}`).join(" ");
  const bestSoFar = [];
  let m = -1; runs.forEach((r) => { if (r.verdict === "keep") m = Math.max(m, r.metric); bestSoFar.push(m); });
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
      {/* gridlines */}
      {[0, 0.25, 0.5, 0.75, 1].map((t, i) => {
        const val = lo + t * (hi - lo);
        return <g key={i}><line x1={pad.l} y1={y(val)} x2={W - pad.r} y2={y(val)} stroke="var(--border)" strokeWidth="0.5" /><text x={pad.l - 8} y={y(val) + 3} textAnchor="end" fontSize="9" fill="var(--text-4)" fontFamily="var(--mono)">{val.toFixed(2)}</text></g>;
      })}
      {/* baseline */}
      <line x1={pad.l} y1={y(baseline)} x2={W - pad.r} y2={y(baseline)} stroke="var(--text-3)" strokeWidth="1" strokeDasharray="4 3" />
      <text x={W - pad.r} y={y(baseline) - 5} textAnchor="end" fontSize="9" fill="var(--text-3)" fontFamily="var(--mono)">baseline {baseline}</text>
      {/* target */}
      <line x1={pad.l} y1={y(target)} x2={W - pad.r} y2={y(target)} stroke="var(--ok)" strokeWidth="1" strokeDasharray="2 3" opacity="0.6" />
      <text x={pad.l} y={y(target) - 5} fontSize="9" fill="var(--ok-tx)" fontFamily="var(--mono)">target &gt;0.80</text>
      {/* line */}
      <polyline points={linePts} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" />
      {/* points */}
      {runs.map((r, i) => (
        <g key={i}>
          <circle cx={x(i)} cy={y(r.metric)} r={r.verdict === "discard" ? 3.5 : 5} fill={r.verdict === "discard" ? "var(--surface)" : r.verdict === "running" ? "var(--ok)" : "var(--accent)"} stroke={r.verdict === "discard" ? "var(--danger)" : "var(--surface)"} strokeWidth="1.5" className={r.verdict === "running" ? "pulse" : ""} />
          <text x={x(i)} y={H - 10} textAnchor="middle" fontSize="8.5" fill="var(--text-4)" fontFamily="var(--mono)">{r.id.replace("run-", "")}</text>
        </g>
      ))}
    </svg>
  );
}

function HypChip({ status }) {
  const map = { confirmed: ["var(--ok-bg)", "var(--ok-tx)", "✓ 验证"], rejected: ["var(--danger-bg)", "var(--danger-tx)", "✗ 证伪"], testing: ["var(--warn-bg)", "var(--warn-tx)", "◴ 测试中"] };
  const [bg, c, t] = map[status] || map.testing;
  return <span className="pill sm" style={{ background: bg, color: c }}>{t}</span>;
}

function ExperimentLab({ openGates }) {
  const D = window.DATA;
  const e = D.experiment;
  const [tab, setTab] = React.useState("run");
  const idea = D.ideas.find(i => i.id === e.idea);

  return (
    <div className="page-wide" style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* header */}
      <div style={{ padding: "20px 28px 0", borderBottom: "0.5px solid var(--border)", background: "var(--app-bg)" }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="h-eyebrow">Skill 4 · experiment-lab</div>
            <h1 style={{ fontSize: 19, fontWeight: 680, margin: "6px 0 4px", letterSpacing: "-0.01em", lineHeight: 1.3 }}>{idea.title}</h1>
            <div className="row gap10">
              <span className="mono muted" style={{ fontSize: 11 }}>{e.idea}</span>
              <SP5 status={idea.status} sm />
              <span className="pill sm"><I5 name="server" size={11} />{e.host}</span>
              <span className="pill sm"><I5 name="cpu" size={11} />{e.scheduler}</span>
            </div>
          </div>
          <div style={{ textAlign: "right", flexShrink: 0 }}>
            <div className="row gap6" style={{ justifyContent: "flex-end", alignItems: "baseline" }}>
              <span className="mono" style={{ fontSize: 22, fontWeight: 700, color: "var(--accent-text)" }}>{e.usedGpuH}</span>
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>/ {e.budgetGpuH} GPU·h</span>
            </div>
            <div className="bar" style={{ width: 150, marginTop: 6 }}><i style={{ width: `${e.usedGpuH / e.budgetGpuH * 100}%` }} /></div>
          </div>
        </div>
        <div className="row" style={{ marginTop: 16, gap: 2 }}>
          {[["plan", "plan 设计"], ["setup", "setup 环境"], ["run", "run 循环"], ["report", "report 回填"]].map(([k, lab]) => (
            <button key={k} onClick={() => setTab(k)} style={{ border: "none", background: "none", cursor: "pointer", padding: "10px 16px", fontSize: 13, fontWeight: 600, fontFamily: "var(--sans)", color: tab === k ? "var(--text)" : "var(--text-3)", borderBottom: tab === k ? "2px solid var(--accent)" : "2px solid transparent" }}>{lab}</button>
          ))}
        </div>
      </div>

      <div className="scroll" style={{ overflowY: "auto", flex: 1, padding: "26px 32px 60px" }}>
        {tab === "plan" && <PlanTab e={e} openGates={openGates} />}
        {tab === "setup" && <SetupTab e={e} openGates={openGates} />}
        {tab === "run" && <RunTab e={e} />}
        {tab === "report" && <ReportTab e={e} idea={idea} />}
      </div>
    </div>
  );
}

function PlanTab({ e, openGates }) {
  return (
    <div className="fadeup" style={{ maxWidth: 920, margin: "0 auto" }}>
      <div className="card card-pad" style={{ marginBottom: 18, background: "var(--surface-2)" }}>
        <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>data/experiments/{e.idea}/program.md</span>
        <div style={{ fontSize: 14, fontWeight: 650, margin: "10px 0 6px" }}>目标 Goal</div>
        <p style={{ fontSize: 13, lineHeight: 1.6, margin: 0, color: "var(--text)" }}>{e.program.goal}</p>
      </div>

      <span className="section-h" style={{ marginBottom: 12 }}><I5 name="sparkle" size={15} style={{ color: "var(--accent)" }} />假设清单 Hypotheses</span>
      <div className="col gap8" style={{ marginBottom: 22 }}>
        {e.program.hypotheses.map((h, i) => (
          <div key={i} className="card" style={{ padding: "12px 16px" }}>
            <div className="row gap12"><span style={{ fontSize: 13, flex: 1 }}>{h.h}</span><HypChip status={h.status} /></div>
          </div>
        ))}
      </div>

      <span className="section-h" style={{ marginBottom: 12 }}><I5 name="layers" size={15} style={{ color: "var(--accent)" }} />Baseline 发现与复现策略</span>
      <div className="card" style={{ overflow: "hidden", marginBottom: 22 }}>
        <table className="tbl">
          <thead><tr><th>对比方法</th><th>复现来源</th><th>状态</th><th>基线值</th><th>备注</th></tr></thead>
          <tbody>
            {e.baselines.map((b, i) => (
              <tr key={i}>
                <td style={{ fontWeight: 600 }}>{b.name}</td>
                <td><span className="pill sm" style={{ background: b.source === "官方代码" ? "var(--ok-bg)" : "var(--warn-bg)", color: b.source === "官方代码" ? "var(--ok-tx)" : "var(--warn-tx)" }}>{b.source}</span></td>
                <td><span className="mono" style={{ fontSize: 11, color: "var(--ok-tx)" }}>✓ {b.repro}</span></td>
                <td className="mono" style={{ fontWeight: 700 }}>{b.value}</td>
                <td className="muted" style={{ fontSize: 11.5 }}>{b.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 11.5, color: "var(--text-3)", marginBottom: 22, lineHeight: 1.5 }}>复现优先级：官方代码 &gt; 第三方可信复现 &gt; 自重写 &gt; 仅引用论文数字（标注非公平）。每 baseline 上限 8 GPU·h。</div>

      <div className="card card-pad" style={{ background: "var(--surface-2)", marginBottom: 18 }}>
        <span className="section-h" style={{ marginBottom: 10 }}><I5 name="clock" size={14} style={{ color: "var(--accent)" }} />停止条件 Stop</span>
        <p style={{ fontSize: 13, margin: 0, color: "var(--text-2)", lineHeight: 1.5 }}>{e.program.stop}</p>
      </div>

      <div className="card card-pad" style={{ background: "var(--accent-soft)" }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div><div style={{ fontSize: 13.5, fontWeight: 650 }}>compute_budget 人闸</div><div style={{ fontSize: 11.5, color: "var(--text-2)", marginTop: 3 }}>消耗真实算力前需确认方案与预算（48 GPU·h）。</div></div>
          <button className="btn btn-primary" onClick={() => openGates("G-20260529-003")} style={{ alignSelf: "center" }}><I5 name="gate" size={14} />确认预算</button>
        </div>
      </div>
    </div>
  );
}

function SetupTab({ e, openGates }) {
  const steps = [
    { t: "remote_exec 解析 --host gpu-node-1", s: "ok", d: "用 ~/.ssh/config alias，零凭据留存" },
    { t: "env_provision 建隔离环境 conda exp-003", s: "ok", d: "rsync env/ + code/ → 远程" },
    { t: "数据可用性检查 /mnt/datasets", s: "ok", d: "size + checksum 校验通过，许可核对 OK" },
    { t: "冒烟测试：最小样例确认 GPU/依赖/数据", s: "ok", d: "1×A100 可见，依赖完整，30s 通过" },
  ];
  return (
    <div className="fadeup" style={{ maxWidth: 820, margin: "0 auto" }}>
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <span className="section-h" style={{ marginBottom: 16 }}><I5 name="server" size={15} style={{ color: "var(--accent)" }} />远程隔离环境搭建</span>
        {steps.map((st, i) => (
          <div key={i} className="row gap12" style={{ padding: "11px 0", borderBottom: i < steps.length - 1 ? "0.5px solid var(--border)" : "none", alignItems: "flex-start" }}>
            <div style={{ width: 22, height: 22, borderRadius: "50%", background: "var(--ok-bg)", color: "var(--ok-tx)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, marginTop: 1 }}><I5 name="check" size={13} /></div>
            <div style={{ flex: 1 }}><div style={{ fontSize: 13, fontWeight: 600 }}>{st.t}</div><div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>{st.d}</div></div>
          </div>
        ))}
      </div>
      <div className="card card-pad" style={{ background: "var(--surface-2)" }}>
        <span className="section-h" style={{ marginBottom: 10 }}><I5 name="shield" size={14} style={{ color: "var(--accent)" }} />安全与隔离</span>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          {["独立 conda env / 容器，数据只读挂载", "写操作（install/rm/sbatch）走 remote_write 人闸", "预算三重硬上限：总额 + 单 baseline + --budget", "凭据零留存，code/ 全程 git 可回滚"].map((t, i) => (
            <div key={i} className="row gap8" style={{ fontSize: 12, color: "var(--text-2)", alignItems: "flex-start" }}><span style={{ marginTop: 5, width: 5, height: 5, borderRadius: "50%", background: "var(--accent)", flexShrink: 0 }} />{t}</div>
          ))}
        </div>
      </div>
    </div>
  );
}

function RunTab({ e }) {
  return (
    <div className="fadeup">
      <div className="row gap20" style={{ alignItems: "stretch", marginBottom: 22 }}>
        <div className="card card-pad" style={{ flex: 1.6 }}>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <span className="section-h"><I5 name="chart" size={15} style={{ color: "var(--accent)" }} />主指标曲线 · {e.metric.name}</span>
            <span className="row gap6"><span className="mono" style={{ fontSize: 20, fontWeight: 700 }}>{e.metric.best}</span><D5>{`+${((e.metric.best - e.metric.baseline) * 100).toFixed(1)}pt`}</D5></span>
          </div>
          <MetricChart runs={e.runs} baseline={e.metric.baseline} target={0.80} />
        </div>
        <div className="card card-pad" style={{ flex: 1 }}>
          <span className="section-h" style={{ marginBottom: 14 }}><I5 name="refresh" size={14} style={{ color: "var(--accent)" }} />Karpathy 循环</span>
          {["提假设（一次只动一个变量）", "edit · 改 code/ 或 config（git 记录）", "run · 远程提交作业 + 流式日志", "measure · 解析指标 → ledger.jsonl", "keep / discard · 优于最优则保留，否则回滚", "反思 · 必要时回写优化 idea 本身"].map((t, i) => (
            <div key={i} className="row gap10" style={{ marginBottom: 9, alignItems: "flex-start" }}>
              <span className="mono" style={{ fontSize: 11, color: "var(--accent-text)", flexShrink: 0, marginTop: 1 }}>{i + 1}</span>
              <span style={{ fontSize: 12, color: "var(--text-2)", lineHeight: 1.4 }}>{t}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ledger */}
      <div className="card" style={{ overflow: "hidden" }}>
        <div className="card-pad row" style={{ justifyContent: "space-between", paddingBottom: 12 }}>
          <span className="section-h"><I5 name="git" size={15} style={{ color: "var(--accent)" }} />实验账本 ledger.jsonl <span className="en-label" style={{ fontSize: 11 }}>append-only</span></span>
          <span className="pill sm" style={{ background: "var(--ok-bg)", color: "var(--ok-tx)" }}><span className="dot pulse" />run-006 running</span>
        </div>
        <table className="tbl">
          <thead><tr><th>run</th><th>config diff（动一个变量）</th><th>{e.metric.name}</th><th>判定</th><th>GPU·h</th><th>时间</th></tr></thead>
          <tbody>
            {e.runs.map((r, i) => {
              const best = Math.max(...e.runs.filter(x => x.verdict === "keep").map(x => x.metric));
              const isBest = r.metric === best && r.verdict === "keep";
              return (
                <tr key={i} className={isBest ? "hl" : ""}>
                  <td className="mono" style={{ fontWeight: 600 }}>{r.id}{isBest && <span style={{ color: "var(--accent-text)", marginLeft: 6 }}>★</span>}</td>
                  <td className="mono" style={{ fontSize: 11.5, color: "var(--text-2)" }}>{r.diff}</td>
                  <td className="mono" style={{ fontWeight: 700 }}>{r.metric.toFixed(3)}</td>
                  <td>{r.verdict === "keep" ? <span className="pill sm" style={{ background: "var(--ok-bg)", color: "var(--ok-tx)" }}>keep</span> : r.verdict === "discard" ? <span className="pill sm" style={{ background: "var(--danger-bg)", color: "var(--danger-tx)" }}>discard ↩</span> : <span className="pill sm" style={{ background: "var(--warn-bg)", color: "var(--warn-tx)" }}><span className="dot pulse" />running</span>}</td>
                  <td className="mono muted" style={{ fontSize: 11.5 }}>{r.gpuH}</td>
                  <td className="mono muted" style={{ fontSize: 11 }}>{r.ts}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ReportTab({ e, idea }) {
  return (
    <div className="fadeup" style={{ maxWidth: 920, margin: "0 auto" }}>
      <div className="card card-pad" style={{ marginBottom: 20, background: "linear-gradient(135deg, var(--surface), var(--accent-soft) 240%)" }}>
        <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>data/experiments/{e.idea}/results.md</span>
        <div className="row gap16" style={{ marginTop: 14, alignItems: "center" }}>
          <div><div style={{ fontSize: 11, color: "var(--text-3)" }}>主指标 {e.metric.name}</div><div className="row gap8" style={{ alignItems: "baseline" }}><span className="mono" style={{ fontSize: 30, fontWeight: 700 }}>{e.metric.best}</span><D5>{`+${((e.metric.best - e.metric.baseline) * 100).toFixed(1)}pt`}</D5></div></div>
          <div className="hr" style={{ width: 1, height: 40, background: "var(--border-2)" }} />
          <div><div style={{ fontSize: 11, color: "var(--text-3)" }}>vs baseline</div><div className="mono" style={{ fontSize: 18, fontWeight: 600, marginTop: 4 }}>{e.metric.baseline}</div></div>
          <div style={{ marginLeft: "auto", textAlign: "right" }}><SP5 status="implemented" /><div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 6 }}>已回填 ideas/{e.idea}.md</div></div>
        </div>
      </div>

      <span className="section-h" style={{ marginBottom: 12 }}><I5 name="layers" size={15} style={{ color: "var(--accent)" }} />消融对比 Ablation</span>
      <div className="card" style={{ overflow: "hidden", marginBottom: 22 }}>
        <table className="tbl">
          <thead><tr><th>配置</th><th>precision</th><th>recall</th><th>vs full</th></tr></thead>
          <tbody>
            {e.ablation.map((a, i) => (
              <tr key={i} className={i === 0 ? "hl" : ""}>
                <td style={{ fontWeight: i === 0 ? 700 : 500 }}>{a.config}{i === 0 && " ★"}</td>
                <td className="mono" style={{ fontWeight: 700 }}>{a.precision.toFixed(3)}</td>
                <td className="mono muted">{a.recall.toFixed(3)}</td>
                <td>{i === 0 ? <span className="muted">—</span> : <span className="delta-down mono">{((a.precision - e.ablation[0].precision) * 100).toFixed(1)}pt</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card card-pad" style={{ background: "var(--surface-2)" }}>
        <span className="section-h" style={{ marginBottom: 10 }}><I5 name="bulb" size={14} style={{ color: "var(--accent)" }} />idea 的修正（实验回写）</span>
        <p style={{ fontSize: 13, lineHeight: 1.6, margin: 0, color: "var(--text-2)" }}>实验证伪了 H3（3-hop 更好）——3-hop 引入噪声反而下降。idea 据此收敛为「2-hop 共引 + 时间衰减 + 度归一化」。这种「idea 在实验中被修正」正是 auto-research 的价值所在。结论已回填至 <W5>{e.idea}</W5> 的「实验结论」节，相关方法沉淀进 concepts/。</p>
      </div>
    </div>
  );
}

window.ExperimentLab = ExperimentLab;
