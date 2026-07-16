import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import {
  api,
  ApiError,
  VOYAGE_TERMINAL,
  type CitationCheck,
  type CitationCheckItem,
  type FactCheckItem,
  type ManuscriptFileMeta,
  type MetaReview,
  type PaperReviewPayload,
  type ReviewGuardrail,
  type ReviewMessageRead,
  type ReviewPersona,
  type ReviewerOpinion,
} from '../../lib/api';
import { DiscussionBubble } from '../review/messages';

/* ============================================================
   /paper-review — Stage 05 · Paper Review（M5-C）
   稿件下拉（compiled/under_review）→ 发起同行评审（personas 可编辑）
   → 总览卡（三维度 + rating + 结论）→ 逐评审员卡 → 引用核验表
   → 查错清单（location 深链 writer）→ 人类讨论区 → 修订/申请投稿。
   历史多轮：GET /manuscripts/{id}/reviews 下拉切换。
   ============================================================ */

/* ---------------- 文案与配色 ---------------- */

const DEFAULT_REVIEW_PERSONAS: ReviewPersona[] = [
  { name: '苛刻方法论者', stance: '专挑方法和实验设计的漏洞，标准从严' },
  { name: '建设性领域专家', stance: '熟悉领域现状，指出问题的同时给出改进建议' },
  { name: '严格实验复现者', stance: '关注实验细节和可复现性，逐个核对数字与设置' },
];

interface PillMeta {
  zh: string;
  bg: string;
  tx: string;
}

const DECISION_META: Record<string, PillMeta> = {
  accept: { zh: '建议接收', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  borderline: { zh: '边缘', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  reject: { zh: '建议拒稿', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
};

const EXISTENCE_META: Record<string, PillMeta> = {
  exact: { zh: '找到了', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  minor: { zh: '基本匹配', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  fabricated: { zh: '疑似编造', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
};

const SUPPORT_META: Record<string, PillMeta> = {
  supported: { zh: '支撑论点', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  partial: { zh: '部分支撑', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  unsupported: { zh: '不支撑', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
  not_checked: { zh: '未核验', bg: 'var(--surface-3)', tx: 'var(--text-3)' },
};

const SOURCE_TEXT: Record<string, string> = {
  library: '本项目文献库',
  s2: 'Semantic Scholar',
  openalex: 'OpenAlex',
  none: '哪里都没找到',
};

const KIND_TEXT: Record<string, string> = {
  number_mismatch: '数字对不上',
  unsupported_claim: '说法缺依据',
  missing_figure: '图表缺失',
  other: '其他',
};

const META_DIMS = [
  { key: 'soundness', zh: '严谨程度', en: 'soundness' },
  { key: 'presentation', zh: '表达清晰', en: 'presentation' },
  { key: 'contribution', zh: '贡献大小', en: 'contribution' },
] as const;

function dimColor(v: number): string {
  return v >= 3 ? 'var(--ok)' : v >= 2 ? 'var(--warn)' : 'var(--danger)';
}

/** 主席 meta 消息判定（author_name = "主席 Meta" 或含 meta）。 */
function isMetaAuthor(name: string): boolean {
  return name.includes('主席') || name.toLowerCase().includes('meta');
}

/** 尝试把 ReviewMessage.content 解析成结构化评审意见（容错 ```json 围栏）。 */
function parseReviewerOpinion(content: string): ReviewerOpinion | null {
  let text = content.trim();
  const fence = /^```(?:json)?\s*([\s\S]*?)\s*```$/.exec(text);
  if (fence) text = fence[1]!;
  if (!text.startsWith('{')) return null;
  try {
    const obj: unknown = JSON.parse(text);
    if (obj && typeof obj === 'object' && ('rating' in obj || 'soundness' in obj || 'strengths' in obj)) {
      return obj as ReviewerOpinion;
    }
  } catch {
    /* 不是 JSON，按 markdown 渲染 */
  }
  return null;
}

/** location 形如 "results.tex:42" 或 "main.tex" 且文件在稿件里 → 可跳编辑器。 */
function isLinkableLocation(loc: string, files: ManuscriptFileMeta[] | undefined): boolean {
  if (!files || files.length === 0) return false;
  const m = /^([\w@./-]+\.\w+)(?::\d+)?$/.exec(loc.trim());
  if (!m) return false;
  const norm = (p: string) => p.replace(/^\.\//, '').replace(/^\//, '');
  const f = norm(m[1]!);
  return files.some((x) => norm(x.path) === f || norm(x.path).endsWith(`/${f}`));
}

/* ---------------- 发起评审 Modal（personas 可编辑，同 M3 锦标赛风格） ---------------- */

function StartReviewModal({
  open,
  onClose,
  msId,
  pid,
}: {
  open: boolean;
  onClose: () => void;
  msId: string;
  pid: string;
}) {
  const queryClient = useQueryClient();
  const [personas, setPersonas] = useState<ReviewPersona[]>(DEFAULT_REVIEW_PERSONAS);

  const mutation = useMutation({
    mutationFn: () => api.startManuscriptReview(msId, personas.filter((p) => p.name.trim() !== '')),
    onSuccess: () => {
      toast('同行评审已开始', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages', pid] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript', msId] });
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        if (e.message.includes('COMPILE_REQUIRED')) {
          toast('稿件要先编译成功一次才能发起评审（去写作页按 ⌘S 编译）', 'error');
        } else {
          toast('这篇稿件已有一轮评审在进行中，等它跑完再发起新一轮。', 'error');
          void queryClient.invalidateQueries({ queryKey: ['voyages', pid] });
        }
      } else {
        toast(`发起失败：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  function setPersona(i: number, patch: Partial<ReviewPersona>) {
    setPersonas((ps) => ps.map((p, pi) => (pi === i ? { ...p, ...patch } : p)));
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={560}
      title={
        <>
          <Icon name="shield" size={16} style={{ color: 'var(--accent)' }} />
          发起同行评审
        </>
      }
      sub="先自动核验全部引用、逐条查错，再由三位 AI 评审员从不同角度打分，最后汇总出结论"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                启动中…
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                开始评审
              </>
            )}
          </button>
        </>
      }
    >
      <FormField
        label="评审员人设"
        en="personas"
        hint="每个人设是一位独立的 AI 评审员：名字 + 评审立场。默认三位可直接编辑或增删。"
      >
        <div className="col gap8">
          {personas.map((p, i) => (
            <div key={i} className="row gap8">
              <input
                className="input"
                style={{ width: 160, flexShrink: 0 }}
                placeholder="名字 name"
                value={p.name}
                onChange={(e) => setPersona(i, { name: e.target.value })}
              />
              <input
                className="input"
                style={{ flex: 1 }}
                placeholder="立场 stance"
                value={p.stance}
                onChange={(e) => setPersona(i, { stance: e.target.value })}
              />
              <button
                className="icon-btn"
                title="移除"
                onClick={() => setPersonas((ps) => ps.filter((_, pi) => pi !== i))}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
          <button
            className="btn btn-soft sm"
            style={{ alignSelf: 'flex-start' }}
            onClick={() => setPersonas((ps) => [...ps, { name: '', stance: '' }])}
          >
            <Icon name="plus" size={13} />
            添加人设
          </button>
        </div>
      </FormField>
      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        评审通过（总评 ≥ 6 且没有编造引用）后才能申请投稿；未通过时，问题清单会自动写进事实包，供下一轮修订参考。
      </div>
    </Modal>
  );
}

/* ---------------- 总览卡 ---------------- */

function DimBar({ zh, en, value }: { zh: string; en: string; value: number | null | undefined }) {
  const v = typeof value === 'number' ? value : null;
  return (
    <div className="row gap10" style={{ marginBottom: 10 }}>
      <span style={{ width: 96, flexShrink: 0, fontSize: 12, color: 'var(--text-2)' }}>
        {zh}
        <span className="en-label" style={{ fontSize: 10, marginLeft: 5 }}>{en}</span>
      </span>
      <div className="row" style={{ flex: 1, gap: 4 }} title={`${zh}: ${v ?? '—'} / 4`}>
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            style={{
              flex: 1,
              height: 8,
              borderRadius: 4,
              background: v != null && v >= i - 0.25 ? dimColor(v) : 'var(--surface-3)',
            }}
          />
        ))}
      </div>
      <span className="mono" style={{ width: 44, textAlign: 'right', fontWeight: 700, fontSize: 12.5 }}>
        {v ?? '—'}
        <span style={{ color: 'var(--text-4)', fontWeight: 400 }}> /4</span>
      </span>
    </div>
  );
}

function MetaOverviewCard({
  meta,
  guardrail,
  fabricated,
  summaryFallback,
}: {
  meta: MetaReview | null;
  guardrail: ReviewGuardrail | null;
  fabricated: number;
  summaryFallback: string | null;
}) {
  const rating = typeof meta?.rating === 'number' ? meta.rating : null;
  const decision = meta?.decision_hint ? DECISION_META[meta.decision_hint] : null;
  const summary = meta?.summary ?? summaryFallback;
  const ratings = meta?.aggregation?.ratings ?? [];
  return (
    <div className="card card-pad" style={{ marginBottom: 16 }}>
      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <span className="section-h">
          <Icon name="shield" size={15} style={{ color: 'var(--accent)' }} />
          评审总览 <span className="en-label" style={{ fontSize: 11 }}>meta-review · 汇总结论</span>
        </span>
        {guardrail && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            意见可靠性自动校验{guardrail.passed === false ? '未全部通过' : '通过'}
            {guardrail.regenerated ? ` · 重写 ${guardrail.regenerated} 次` : ''}
          </span>
        )}
      </div>
      <div className="row gap16" style={{ alignItems: 'stretch', flexWrap: 'wrap' }}>
        {/* rating 大数字 + 结论 pill */}
        <div
          className="col"
          style={{
            minWidth: 150,
            alignItems: 'center',
            justifyContent: 'center',
            gap: 6,
            padding: '10px 18px',
            borderRadius: 12,
            background: 'var(--surface-2)',
          }}
        >
          <div className="mono" style={{ fontSize: 42, fontWeight: 750, lineHeight: 1, color: 'var(--accent-text)' }}>
            {rating != null ? rating : '—'}
            <span style={{ fontSize: 15, color: 'var(--text-4)', fontWeight: 500 }}> /10</span>
          </div>
          <div style={{ fontSize: 10.5, color: 'var(--text-3)' }}>总评 rating（三位评审员聚合）</div>
          {decision && (
            <span className="pill" style={{ background: decision.bg, color: decision.tx }}>
              <span className="dot" />
              {decision.zh}
            </span>
          )}
        </div>
        {/* 三维度条形 1-4 */}
        <div style={{ flex: 1, minWidth: 280, alignSelf: 'center' }}>
          {META_DIMS.map((d) => (
            <DimBar key={d.key} zh={d.zh} en={d.en} value={meta?.[d.key]} />
          ))}
          <div className="row gap8" style={{ marginTop: 4, flexWrap: 'wrap' }}>
            {ratings.length > 0 && (
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                各评审员总评：{ratings.join(' / ')} · 取中位数，跑偏和低把握的意见降权
              </span>
            )}
            {fabricated > 0 && (
              <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>
                有 {fabricated} 条疑似编造引用，结论强制不通过
              </span>
            )}
          </div>
        </div>
      </div>
      {summary && (
        <div style={{ marginTop: 14, paddingTop: 14, borderTop: '0.5px solid var(--border)' }}>
          <Markdown source={summary} style={{ fontSize: 13 }} />
        </div>
      )}
    </div>
  );
}

/* ---------------- 逐评审员卡 ---------------- */

function MiniScale({ zh, value, max }: { zh: string; value: number | undefined; max: number }) {
  const v = typeof value === 'number' ? value : null;
  const pct = v == null ? 0 : Math.max(0, Math.min(100, (v / max) * 100));
  return (
    <div style={{ flex: 1, minWidth: 0 }} title={`${zh}: ${v ?? '—'} / ${max}`}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ fontSize: 10, color: 'var(--text-3)' }}>{zh}</span>
        <span className="mono" style={{ fontSize: 10, fontWeight: 700 }}>
          {v ?? '—'}<span style={{ color: 'var(--text-4)', fontWeight: 400 }}>/{max}</span>
        </span>
      </div>
      <div className="bar" style={{ height: 5 }}>
        <i style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function OpinionZone({ title, items, bg, tx }: { title: string; items: string[] | undefined; bg: string; tx: string }) {
  if (!items || items.length === 0) return null;
  return (
    <div style={{ background: bg, borderRadius: 9, padding: '8px 12px', marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: tx, marginBottom: 4 }}>{title}</div>
      <ul style={{ margin: 0, paddingLeft: 16 }}>
        {items.map((s, i) => (
          <li key={i} style={{ fontSize: 12, lineHeight: 1.55, color: 'var(--text-2)', marginBottom: 3 }}>{s}</li>
        ))}
      </ul>
    </div>
  );
}

function ReviewerCard({ msg }: { msg: ReviewMessageRead }) {
  const op = parseReviewerOpinion(msg.content);
  const unreliable = op?.unreliable === true;
  return (
    <div
      className="card card-pad"
      style={{
        opacity: unreliable ? 0.55 : 1,
        borderColor: unreliable ? 'var(--border-2)' : undefined,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div className="row gap8" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          <Icon name="users" size={12} />
          {msg.author_name}
        </span>
        {op && typeof op.confidence === 'number' && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }} title="评审员对自己判断的把握程度">
            把握 {op.confidence}/5
          </span>
        )}
        {unreliable && (
          <span
            className="pill sm"
            style={{ background: 'var(--surface-3)', color: 'var(--text-3)', marginLeft: 'auto' }}
            title="这份意见没通过可靠性自动校验（内容不够具体或与论文对不上），已灰显且不计入汇总分数"
          >
            未通过可靠性校验 · 不计入汇总
          </span>
        )}
      </div>
      {op ? (
        <>
          <div className="row gap10" style={{ marginBottom: 4 }}>
            <MiniScale zh="严谨" value={op.soundness} max={4} />
            <MiniScale zh="表达" value={op.presentation} max={4} />
            <MiniScale zh="贡献" value={op.contribution} max={4} />
            <MiniScale zh="总评" value={op.rating} max={10} />
          </div>
          <OpinionZone title="优点 strengths" items={op.strengths} bg="var(--ok-bg)" tx="var(--ok-tx)" />
          <OpinionZone title="缺点 weaknesses" items={op.weaknesses} bg="var(--danger-bg)" tx="var(--danger-tx)" />
          <OpinionZone title="提问 questions" items={op.questions} bg="var(--warn-bg)" tx="var(--warn-tx)" />
        </>
      ) : (
        <Markdown source={msg.content} style={{ fontSize: 12.5 }} />
      )}
    </div>
  );
}

/* ---------------- 引用核验表 ---------------- */

const CIT_GRID = 'minmax(110px, 170px) 92px 150px 92px minmax(0, 1fr)';

function CitationRow({ item }: { item: CitationCheckItem }) {
  const ex = EXISTENCE_META[item.existence] ?? { zh: item.existence, bg: 'var(--surface-3)', tx: 'var(--text-3)' };
  const sp = item.support ? SUPPORT_META[item.support] : null;
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: CIT_GRID,
        gap: 12,
        alignItems: 'center',
        padding: '9px 18px',
        borderBottom: '0.5px solid var(--border)',
      }}
      title={item.context_snippet ? `引用语境：${item.context_snippet}` : undefined}
    >
      <span className="mono" style={{ fontSize: 11.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {item.bibkey}
      </span>
      <span className="pill sm" style={{ background: ex.bg, color: ex.tx, justifySelf: 'start' }}>{ex.zh}</span>
      <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{SOURCE_TEXT[item.source ?? ''] ?? item.source ?? '—'}</span>
      {sp ? (
        <span className="pill sm" style={{ background: sp.bg, color: sp.tx, justifySelf: 'start' }}>{sp.zh}</span>
      ) : (
        <span className="muted mono" style={{ fontSize: 11 }}>—</span>
      )}
      <span
        style={{
          fontSize: 11.5,
          color: 'var(--text-2)',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {item.matched_title ?? <span className="muted">（没匹配到论文）</span>}
      </span>
    </div>
  );
}

function CitationCard({ check }: { check: CitationCheck | null }) {
  const items = check?.items ?? [];
  const total = check?.total ?? items.length;
  const fabricated = items.filter((i) => i.existence === 'fabricated').length;
  const unsupported = items.filter((i) => i.support === 'unsupported').length;
  return (
    <div className="card" style={{ marginBottom: 16, overflow: 'hidden' }}>
      <div className="card-pad row gap10" style={{ paddingBottom: 12, flexWrap: 'wrap' }}>
        <span className="section-h">
          <Icon name="link" size={15} style={{ color: 'var(--accent)' }} />
          引用核验 <span className="en-label" style={{ fontSize: 11 }}>citation check · 每条引用都查过一遍</span>
        </span>
        <span className="row gap8" style={{ marginLeft: 'auto', flexWrap: 'wrap' }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{total} 条引用</span>
          {fabricated > 0 ? (
            <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>
              <Icon name="bell" size={11} />
              {fabricated} 条疑似编造
            </span>
          ) : (
            items.length > 0 && (
              <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>没有编造引用</span>
            )
          )}
          {unsupported > 0 && (
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              {unsupported} 条不支撑论点
            </span>
          )}
        </span>
      </div>
      {items.length === 0 ? (
        <div className="empty" style={{ padding: 24, fontSize: 12 }}>这轮评审没有返回引用核验明细。</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <div style={{ minWidth: 640 }}>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: CIT_GRID,
                gap: 12,
                padding: '8px 18px',
                borderTop: '0.5px solid var(--border)',
                borderBottom: '0.5px solid var(--border)',
                fontSize: 10.5,
                fontWeight: 650,
                color: 'var(--text-3)',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
              }}
            >
              <span>bibkey</span>
              <span>存在性</span>
              <span>匹配来源</span>
              <span>支撑性</span>
              <span>匹配到的论文（悬停行可看引用语境）</span>
            </div>
            {items.map((it, i) => (
              <CitationRow key={`${it.bibkey}-${i}`} item={it} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------- 查错清单 ---------------- */

function FactRow({ item, msId, files }: { item: FactCheckItem; msId: string; files: ManuscriptFileMeta[] | undefined }) {
  const [open, setOpen] = useState(false);
  const major = item.severity === 'major';
  const loc = (item.location ?? '').trim();
  const linkable = loc !== '' && isLinkableLocation(loc, files);
  const hasEvidence = !!item.evidence;
  return (
    <div style={{ borderBottom: '0.5px solid var(--border)' }}>
      <div
        className="row gap8"
        onClick={() => hasEvidence && setOpen((o) => !o)}
        style={{ padding: '9px 18px', alignItems: 'flex-start', cursor: hasEvidence ? 'pointer' : 'default' }}
        title={hasEvidence ? '点击展开/收起依据' : undefined}
      >
        <span
          title={major ? '严重问题 major：必须修' : '小问题 minor：建议修'}
          style={{
            width: 16,
            height: 16,
            borderRadius: '50%',
            flexShrink: 0,
            marginTop: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: major ? 'var(--danger-bg)' : 'var(--warn-bg)',
            color: major ? 'var(--danger-tx)' : 'var(--warn-tx)',
            fontSize: 10,
            fontWeight: 800,
            lineHeight: 1,
          }}
        >
          {major ? '✕' : '!'}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap8" style={{ flexWrap: 'wrap' }}>
            {loc !== '' &&
              (linkable ? (
                <Link
                  to={`/writer/${msId}?goto=${encodeURIComponent(loc)}`}
                  className="mono"
                  onClick={(e) => e.stopPropagation()}
                  title="在写作编辑器里打开对应位置"
                  style={{ fontSize: 10.5, color: 'var(--accent-text)' }}
                >
                  {loc} ↗
                </Link>
              ) : (
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{loc}</span>
              ))}
            {item.kind && (
              <span className="pill sm" style={{ height: 16, fontSize: 9.5, padding: '0 6px' }}>
                {KIND_TEXT[item.kind] ?? item.kind}
              </span>
            )}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.55, marginTop: 2, overflowWrap: 'break-word' }}>
            {item.issue ?? '（未说明问题）'}
          </div>
          {open && hasEvidence && (
            <div
              className="mono"
              style={{
                marginTop: 6,
                padding: '8px 10px',
                borderRadius: 8,
                background: 'var(--surface-2)',
                fontSize: 11,
                color: 'var(--text-2)',
                lineHeight: 1.6,
                whiteSpace: 'pre-wrap',
                overflowWrap: 'break-word',
              }}
            >
              依据：{item.evidence}
            </div>
          )}
        </div>
        {hasEvidence && (
          <Icon
            name="chevDown"
            size={13}
            style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 3, transform: open ? 'rotate(180deg)' : 'none' }}
          />
        )}
      </div>
    </div>
  );
}

function FactCheckCard({
  items,
  msId,
  files,
}: {
  items: FactCheckItem[];
  msId: string;
  files: ManuscriptFileMeta[] | undefined;
}) {
  const major = items.filter((i) => i.severity === 'major').length;
  const minor = items.length - major;
  return (
    <div className="card" style={{ marginBottom: 16, overflow: 'hidden' }}>
      <div className="card-pad row gap10" style={{ paddingBottom: 12 }}>
        <span className="section-h">
          <Icon name="search" size={15} style={{ color: 'var(--accent)' }} />
          查错清单 <span className="en-label" style={{ fontSize: 11 }}>fact check · 数字/说法/图表逐条核对</span>
        </span>
        <span className="row gap8" style={{ marginLeft: 'auto' }}>
          {major > 0 && (
            <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>{major} 个严重</span>
          )}
          {minor > 0 && (
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>{minor} 个轻微</span>
          )}
          {items.length === 0 && (
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>没查出问题</span>
          )}
        </span>
      </div>
      {items.length === 0 ? (
        <div className="empty" style={{ padding: 24, fontSize: 12 }}>数字、说法和图表引用都核对过，没有发现问题。</div>
      ) : (
        <div style={{ borderTop: '0.5px solid var(--border)' }}>
          {items.map((it, i) => (
            <FactRow key={i} item={it} msId={msId} files={files} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- 人类讨论区（复用 DiscussionPanel 模式，target=评审 session） ---------------- */

function ReviewDiscussion({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement | null>(null);

  const messagesQuery = useQuery({
    queryKey: ['session-messages', sessionId],
    queryFn: () => api.listSessionMessages(sessionId),
    retry: false,
    // WS review.message 为主（AppShell 直写 cache），轮询兜底
    refetchInterval: 30_000,
  });
  // 评审员意见 / 主席汇总已在上方结构化展示，这里只显示人类讨论（和 agent 的非结构化回复）
  const messages = (messagesQuery.data ?? []).filter(
    (m) => m.author_type === 'human' || (!isMetaAuthor(m.author_name) && parseReviewerOpinion(m.content) === null),
  );

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const sendMutation = useMutation({
    mutationFn: (content: string) => api.postSessionMessage(sessionId, content),
    onSuccess: (msg) => {
      setDraft('');
      queryClient.setQueryData<ReviewMessageRead[]>(['session-messages', sessionId], (old) =>
        old === undefined ? [msg] : old.some((m) => m.id === msg.id) ? old : [...old, msg],
      );
    },
    onError: (e) => toast(`发送失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  function send() {
    const content = draft.trim();
    if (!content || sendMutation.isPending) return;
    sendMutation.mutate(content);
  }

  return (
    <div className="card" style={{ marginBottom: 16, overflow: 'hidden' }}>
      <div className="card-pad row" style={{ paddingBottom: 12, justifyContent: 'space-between' }}>
        <span className="section-h">
          <Icon name="users" size={15} style={{ color: 'var(--accent)' }} />
          讨论区 <span className="en-label" style={{ fontSize: 11 }}>Discussion · 对这轮评审发表看法</span>
        </span>
        {messages.length > 0 && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{messages.length} 条</span>
        )}
      </div>
      <div
        className="row gap8"
        style={{
          margin: '0 22px 12px',
          padding: '8px 12px',
          borderRadius: 9,
          background: 'var(--accent-soft)',
          fontSize: 11.5,
          color: 'var(--accent-text)',
          lineHeight: 1.5,
        }}
      >
        <Icon name="sparkle" size={13} style={{ flexShrink: 0 }} />
        你的评论会记录在这轮评审里，修订和下一轮评审都能参考 · comments are kept with this review round
      </div>
      <div ref={listRef} className="scroll" style={{ maxHeight: 340, overflowY: 'auto', padding: '4px 22px 8px' }}>
        {messagesQuery.isLoading ? (
          <div className="empty" style={{ padding: 24 }}>加载讨论…</div>
        ) : messagesQuery.isError ? (
          <div className="empty" style={{ padding: 24 }}>无法加载讨论区（后端不可用或接口未就绪）</div>
        ) : messages.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>还没有讨论 — 对评审意见有异议或补充，写在这里</div>
        ) : (
          messages.map((m) => <DiscussionBubble key={m.id} msg={m} />)
        )}
      </div>
      <div className="row gap10" style={{ padding: '12px 22px 18px', borderTop: '0.5px solid var(--border)' }}>
        <textarea
          className="textarea"
          rows={2}
          placeholder="写下你的看法…（Enter 发送，Shift+Enter 换行）"
          value={draft}
          disabled={sendMutation.isPending}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          style={{ flex: 1, minHeight: 44 }}
        />
        <button
          className="btn btn-primary"
          disabled={!draft.trim() || sendMutation.isPending}
          onClick={send}
          style={{ alignSelf: 'flex-end' }}
        >
          {sendMutation.isPending ? (
            <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <Icon name="arrow" size={14} />
          )}
          发送
        </button>
      </div>
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function PaperReviewPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const [msId, setMsId] = useState<string | null>(null);
  const [roundSid, setRoundSid] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  // —— 稿件列表（只保留可评审状态：已编译 / 评审中） ——
  const manuscriptsQuery = useQuery({
    queryKey: ['manuscripts', pid],
    queryFn: () => api.listManuscripts(pid!),
    enabled: !!pid,
    retry: false,
  });
  const reviewable = useMemo(
    () => (manuscriptsQuery.data ?? []).filter((m) => m.status === 'compiled' || m.status === 'under_review'),
    [manuscriptsQuery.data],
  );

  // 自动选中第一篇；项目切换后校正
  useEffect(() => {
    if (reviewable.length === 0) {
      setMsId(null);
      return;
    }
    if (!msId || !reviewable.some((m) => m.id === msId)) {
      setMsId(reviewable[0]!.id);
      setRoundSid(null);
    }
  }, [reviewable, msId]);

  // —— 稿件详情（review_passed + files 供查错清单跳转） ——
  const detailQuery = useQuery({
    queryKey: ['manuscript', msId],
    queryFn: () => api.getManuscript(msId!),
    enabled: !!msId,
    retry: false,
  });
  const detail = detailQuery.data;

  // —— 进行中的评审任务（kind=paper_review，同稿件互斥） ——
  const voyagesQuery = useQuery({
    queryKey: ['voyages', pid],
    queryFn: () => api.listVoyages(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((v) => v.kind === 'paper_review' && !VOYAGE_TERMINAL.has(v.status)) ? 5_000 : false,
  });
  const runningReview =
    (voyagesQuery.data ?? []).find((v) => v.kind === 'paper_review' && !VOYAGE_TERMINAL.has(v.status)) ?? null;

  // 评审任务结束 → 刷新评审列表 / 稿件
  const runningId = runningReview?.id ?? null;
  const prevRunning = useRef<string | null>(null);
  useEffect(() => {
    if (prevRunning.current && !runningId) {
      void queryClient.invalidateQueries({ queryKey: ['manuscript-reviews'] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript'] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
    }
    prevRunning.current = runningId;
  }, [runningId, queryClient]);

  // —— 评审历史（多轮） ——
  const reviewsQuery = useQuery({
    queryKey: ['manuscript-reviews', msId],
    queryFn: () => api.listManuscriptReviews(msId!),
    enabled: !!msId,
    retry: false,
  });
  const reviews = useMemo(
    () => [...(reviewsQuery.data ?? [])].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? '')),
    [reviewsQuery.data],
  );
  const selected = reviews.find((r) => r.session_id === roundSid) ?? reviews[0] ?? null;
  const roundNo = selected ? reviews.length - reviews.indexOf(selected) : 0;

  // payload 容错：完整 payload 或仅 meta 摘要
  const payload: PaperReviewPayload = selected?.payload ?? { meta: selected?.meta ?? null };
  const meta = payload.meta ?? selected?.meta ?? null;
  const citation = payload.citation_check ?? null;
  const factItems = payload.fact_check?.items ?? [];
  const fabricated = (citation?.items ?? []).filter((i) => i.existence === 'fabricated').length;

  // —— 该轮 session 消息（评审员意见 + meta + 人类讨论共用一个 cache key） ——
  const sid = selected?.session_id ?? null;
  const messagesQuery = useQuery({
    queryKey: ['session-messages', sid],
    queryFn: () => api.listSessionMessages(sid!),
    enabled: !!sid,
    retry: false,
    refetchInterval: 30_000,
  });
  const agentMsgs = (messagesQuery.data ?? []).filter((m) => m.author_type === 'agent');
  const metaMsg = agentMsgs.find((m) => isMetaAuthor(m.author_name)) ?? null;
  const reviewerMsgs = agentMsgs.filter((m) => m !== metaMsg && parseReviewerOpinion(m.content) !== null);
  // 全部解析失败时兜底：非 meta 的 agent 消息都当评审员卡（markdown 渲染）
  const reviewerCards = reviewerMsgs.length > 0 ? reviewerMsgs : agentMsgs.filter((m) => m !== metaMsg);

  // —— 评审是否通过（后端 review_passed 优先，缺失时按契约口径推算） ——
  const rating = typeof meta?.rating === 'number' ? meta.rating : null;
  const computedPassed = rating != null && rating >= 6 && fabricated === 0;
  const reviewPassed = selected ? (detail?.review_passed ?? computedPassed) : false;

  // —— 申请投稿 ——
  const submitMutation = useMutation({
    mutationFn: () => api.submitManuscript(msId!),
    onSuccess: () => {
      toast('已提交投稿审批，人工批准后标记为已投稿', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', msId] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        if (e.message.includes('REVIEW_REQUIRED')) {
          toast('要先通过同行评审（总评 ≥ 6 且无编造引用）才能投稿', 'error');
        } else if (e.message.includes('COMPILE_REQUIRED')) {
          toast('要先编译成功一次才能投稿（去写作页按 ⌘S 编译）', 'error');
        } else {
          toast(`投稿失败：${e.message}`, 'error');
        }
      } else {
        toast(`投稿失败：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  function onRevise() {
    if (!msId) return;
    if (!reviewPassed) {
      toast('修订说明已加入事实包，AI 起草/修订时会自动参考', 'info');
    }
    navigate(`/writer/${msId}`);
  }

  /* ---------------- 渲染 ---------------- */

  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Stage 05 · Paper Review"
          title="论文评审 Paper Review"
          sub="先核验引用、逐条查错，再由三位 AI 评审员打分，汇总出接收/拒稿建议。"
        />
        <div className="card">
          <EmptyState
            icon="shield"
            title="还没有研究方向"
            desc="先创建研究方向，写出论文稿件并编译成功后，才能发起同行评审。"
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                新建研究方向 · New direction
              </button>
            }
          />
        </div>
      </div>
    );
  }

  const noManuscripts = !manuscriptsQuery.isLoading && !manuscriptsQuery.isError && reviewable.length === 0;

  return (
    <div className="page fadeup" style={{ maxWidth: 1280 }}>
      <PageHead
        eyebrow="Stage 05 · Paper Review"
        title="论文评审 Paper Review"
        sub={
          currentProject
            ? `当前方向：${currentProject.name}`
            : projectsLoading
              ? '加载研究方向…'
              : '选择一个研究方向'
        }
        en="peer review · citation & fact check"
        right={
          <button
            className="btn btn-primary"
            disabled={!msId || !!runningReview}
            title={!msId ? '先选择一篇稿件' : runningReview ? '已有评审任务在进行中' : '发起一轮同行评审'}
            onClick={() => setModalOpen(true)}
          >
            {runningReview ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                评审中…
              </>
            ) : (
              <>
                <Icon name="shield" size={14} />
                发起同行评审
              </>
            )}
          </button>
        }
      />

      {/* —— 稿件 + 评审轮次选择 —— */}
      <div className="card card-pad row gap10" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)', flexShrink: 0 }}>选择稿件</span>
        <select
          className="input"
          style={{ maxWidth: 420 }}
          value={msId ?? ''}
          disabled={reviewable.length === 0}
          onChange={(e) => {
            setMsId(e.target.value);
            setRoundSid(null);
          }}
        >
          <option value="" disabled>
            {manuscriptsQuery.isLoading ? '加载稿件…' : manuscriptsQuery.isError ? '（稿件列表不可用）' : '— 选择稿件 —'}
          </option>
          {reviewable.map((m) => (
            <option key={m.id} value={m.id}>
              {m.title}（{m.status === 'compiled' ? '已编译' : '评审中'}）
            </option>
          ))}
        </select>
        {msId && (
          <Link to={`/writer/${msId}`} className="mono" style={{ fontSize: 11, color: 'var(--accent-text)' }}>
            在编辑器打开 ↗
          </Link>
        )}
        {reviews.length > 0 && (
          <span className="row gap8" style={{ marginLeft: 'auto' }}>
            <span style={{ fontSize: 12, color: 'var(--text-3)', flexShrink: 0 }}>评审轮次</span>
            <select
              className="input"
              style={{ width: 220 }}
              value={selected?.session_id ?? ''}
              onChange={(e) => setRoundSid(e.target.value)}
            >
              {reviews.map((r, i) => (
                <option key={r.session_id} value={r.session_id}>
                  第 {reviews.length - i} 轮 · {fmtTime(r.created_at)}
                  {i === 0 ? '（最新）' : ''}
                </option>
              ))}
            </select>
          </span>
        )}
      </div>

      {/* —— 进行中的评审任务 —— */}
      {runningReview && (
        <div
          className="card card-pad hoverable"
          onClick={() => navigate(`/voyages/${runningReview.id}`)}
          style={{ marginBottom: 16, borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
        >
          <div className="row gap10">
            <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <span className="dot pulse" />
              评审中
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650 }}>
              同行评审进行中：核验引用 → 查错 → 评审员打分 → 汇总 — 点击看实时进度
            </span>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 'auto' }}>
              任务 {runningReview.id.slice(0, 8)}…
            </span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      {/* —— 主体 —— */}
      {!pid ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{projectsLoading ? '加载研究方向…' : '请先选择研究方向'}</div>
        </div>
      ) : noManuscripts ? (
        <div className="card">
          <EmptyState
            icon="pen"
            title="还没有可评审的稿件"
            desc="同行评审只对编译成功的稿件开放。先在论文撰写页完成编译，再回来发起评审。"
            action={
              <button className="btn btn-ghost" onClick={() => navigate('/writer')}>
                <Icon name="pen" size={14} />
                去写论文
              </button>
            }
          />
        </div>
      ) : manuscriptsQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title="无法加载稿件列表"
            desc="后端不可用或接口尚未就绪。"
            action={
              <button className="btn btn-soft sm" onClick={() => void manuscriptsQuery.refetch()}>重试 retry</button>
            }
          />
        </div>
      ) : !msId ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>加载稿件…</div>
        </div>
      ) : reviewsQuery.isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>加载评审记录…</div>
        </div>
      ) : reviewsQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title="无法加载评审记录"
            desc="后端不可用或接口尚未就绪。"
            action={<button className="btn btn-soft sm" onClick={() => void reviewsQuery.refetch()}>重试 retry</button>}
          />
        </div>
      ) : !selected ? (
        <div className="card">
          <EmptyState
            icon="shield"
            title="这篇稿件还没评审过"
            desc="发起同行评审：自动核验每条引用、逐条查数字和说法的错误，再由三位不同立场的 AI 评审员打分，最后汇总出接收/拒稿建议。"
            action={
              <button className="btn btn-primary" disabled={!!runningReview} onClick={() => setModalOpen(true)}>
                <Icon name="shield" size={14} />
                发起同行评审
              </button>
            }
          />
        </div>
      ) : (
        <>
          {/* 总览 */}
          <MetaOverviewCard
            meta={meta}
            guardrail={payload.guardrail ?? null}
            fabricated={fabricated}
            summaryFallback={metaMsg?.content ?? null}
          />

          {/* 逐评审员 */}
          <div style={{ marginBottom: 16 }}>
            <div className="row" style={{ marginBottom: 10, justifyContent: 'space-between' }}>
              <span className="section-h">
                <Icon name="users" size={15} style={{ color: 'var(--accent)' }} />
                评审员意见 <span className="en-label" style={{ fontSize: 11 }}>reviewers · 三位不同立场</span>
              </span>
              {roundNo > 0 && (
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  第 {roundNo} 轮 · {fmtTime(selected.created_at)}
                </span>
              )}
            </div>
            {messagesQuery.isLoading ? (
              <div className="card">
                <div className="empty" style={{ padding: 30 }}>加载评审员意见…</div>
              </div>
            ) : messagesQuery.isError ? (
              <div className="card">
                <div className="empty" style={{ padding: 30 }}>无法加载评审员意见（后端不可用或接口未就绪）</div>
              </div>
            ) : reviewerCards.length === 0 ? (
              <div className="card">
                <div className="empty" style={{ padding: 30 }}>这轮评审还没有评审员意见。</div>
              </div>
            ) : (
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
                  gap: 14,
                  alignItems: 'stretch',
                }}
              >
                {reviewerCards.map((m) => (
                  <ReviewerCard key={m.id} msg={m} />
                ))}
              </div>
            )}
          </div>

          {/* 引用核验 */}
          <CitationCard check={citation} />

          {/* 查错清单 */}
          <FactCheckCard items={factItems} msId={msId} files={detail?.files} />

          {/* 人类讨论 */}
          {sid && <ReviewDiscussion sessionId={sid} />}

          {/* 底部操作 */}
          <div className="card card-pad row gap10" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
            <div className="row gap8" style={{ fontSize: 12.5, color: 'var(--text-2)', minWidth: 0 }}>
              {reviewPassed ? (
                <>
                  <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                    <Icon name="check" size={12} />
                    评审已通过
                  </span>
                  <span>可以申请投稿，或继续修订打磨。</span>
                </>
              ) : (
                <>
                  <span className="pill" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
                    <Icon name="bell" size={12} />
                    评审未通过
                  </span>
                  <span>缺点和查错清单已自动写成修订说明、加入事实包；修订后可再发起一轮评审。</span>
                </>
              )}
            </div>
            <div className="row gap10">
              <button className="btn btn-ghost" onClick={onRevise}>
                <Icon name="pen" size={14} />
                去修订
              </button>
              <button
                className="btn btn-primary"
                disabled={!reviewPassed || submitMutation.isPending}
                title={reviewPassed ? '发起投稿审批' : '要先通过同行评审（总评 ≥ 6 且无编造引用）才能投稿'}
                onClick={() => submitMutation.mutate()}
              >
                <Icon name="arrow" size={14} />
                {submitMutation.isPending ? '提交中…' : '申请投稿'}
              </button>
            </div>
          </div>
        </>
      )}

      {msId && pid && (
        <StartReviewModal open={modalOpen} onClose={() => setModalOpen(false)} msId={msId} pid={pid} />
      )}
    </div>
  );
}
