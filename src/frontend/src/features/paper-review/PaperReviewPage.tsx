import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
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

/** 默认审稿人（渲染时求值，随语言切换）。 */
function defaultReviewPersonas(): ReviewPersona[] {
  return [
    { name: tr('严格的方法审稿人', 'Strict methods reviewer'), stance: tr('从严挑方法和实验设计的漏洞', 'Holds method and experiment design to a high bar') },
    { name: tr('领域专家', 'Domain expert'), stance: tr('指出问题并给出改进建议', 'Points out problems and suggests fixes') },
    { name: tr('复现审稿人', 'Reproducibility reviewer'), stance: tr('逐项核对实验细节、数字和设置', 'Checks experiment details, numbers and settings one by one') },
  ];
}

interface PillMeta {
  zh: string;
  en?: string;
  bg: string;
  tx: string;
  tone?: Tone;
}

type Tone = 'ok' | 'warn' | 'danger';

/** 状态：8px 圆点 + 文字（不用彩色胶囊）。 */
function StatusText({ tone, children }: { tone: Tone | undefined; children: React.ReactNode }) {
  return (
    <span className="row gap6" style={{ fontSize: 12, color: 'var(--text-2)', whiteSpace: 'nowrap' }}>
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          flexShrink: 0,
          background: tone ? `var(--${tone})` : 'var(--text-4)',
        }}
      />
      {children}
    </span>
  );
}

const DECISION_META: Record<string, PillMeta> = {
  accept: { zh: '建议接收', en: 'Accept', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)', tone: 'ok' },
  borderline: { zh: '边缘', en: 'Borderline', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)', tone: 'warn' },
  reject: { zh: '建议拒稿', en: 'Reject', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)', tone: 'danger' },
};

const EXISTENCE_META: Record<string, PillMeta> = {
  exact: { zh: '已找到', en: 'Found', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  minor: { zh: '基本匹配', en: 'Close match', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  fabricated: { zh: '疑似编造', en: 'Possibly fabricated', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
};

const SUPPORT_META: Record<string, PillMeta> = {
  supported: { zh: '支撑论点', en: 'Supports claim', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  partial: { zh: '部分支撑', en: 'Partially supports', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  unsupported: { zh: '不支撑', en: 'Unsupported', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
  not_checked: { zh: '未核验', en: 'Not checked', bg: 'var(--surface-3)', tx: 'var(--text-3)' },
};

const SOURCE_TEXT: Record<string, { zh: string; en?: string }> = {
  library: { zh: '课题文献库', en: 'Topic library' },
  s2: { zh: 'Semantic Scholar' },
  openalex: { zh: 'OpenAlex' },
  none: { zh: '未找到', en: 'Not found' },
};

const KIND_TEXT: Record<string, { zh: string; en?: string }> = {
  number_mismatch: { zh: '数字不一致', en: 'Number mismatch' },
  unsupported_claim: { zh: '缺少依据', en: 'Unsupported claim' },
  missing_figure: { zh: '图表缺失', en: 'Missing figure' },
  other: { zh: '其他', en: 'Other' },
};

const META_DIMS = [
  { key: 'soundness', zh: '严谨性', en: 'Soundness' },
  { key: 'presentation', zh: '表达', en: 'Presentation' },
  { key: 'contribution', zh: '贡献', en: 'Contribution' },
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
  const [personas, setPersonas] = useState<ReviewPersona[]>(defaultReviewPersonas);

  const mutation = useMutation({
    mutationFn: () => api.startManuscriptReview(msId, personas.filter((p) => p.name.trim() !== '')),
    onSuccess: () => {
      toast(tr('已开始同行评审', 'Peer review started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages', pid] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript', msId] });
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        if (e.message.includes('COMPILE_REQUIRED')) {
          toast(
            tr('请先成功编译稿件，可在编辑器中按 ⌘S', 'Compile the manuscript first. Press ⌘S in the editor.'),
            'error',
          );
        } else {
          toast(
            tr('这篇稿件正在评审中，请等它完成', 'This manuscript is already being reviewed. Wait for it to finish.'),
            'error',
          );
          void queryClient.invalidateQueries({ queryKey: ['voyages', pid] });
        }
      } else {
        toast(`${tr('无法开始评审：', 'Couldn’t start the review: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
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
          {tr('发起同行评审', 'Start peer review')}
        </>
      }
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('启动中…', 'Starting…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('开始评审', 'Start review')}
              </>
            )}
          </button>
        </>
      }
    >
      <FormField
        label={tr('AI 审稿人', 'AI reviewers')}
        hint={tr('每行一位审稿人，可修改或增删', 'One reviewer per row. Edit, add or remove.')}
      >
        <div className="col gap8">
          {personas.map((p, i) => (
            <div key={i} className="row gap8">
              <input
                className="input"
                style={{ width: 160, flexShrink: 0 }}
                placeholder={tr('名称', 'Name')}
                value={p.name}
                onChange={(e) => setPersona(i, { name: e.target.value })}
              />
              <input
                className="input"
                style={{ flex: 1 }}
                placeholder={tr('关注点', 'Focus')}
                value={p.stance}
                onChange={(e) => setPersona(i, { stance: e.target.value })}
              />
              <button
                className="icon-btn"
                title={tr('移除', 'Remove')}
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
            {tr('添加审稿人', 'Add reviewer')}
          </button>
        </div>
      </FormField>
      <div style={{ fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6 }}>
        {tr(
          '总评不低于 6 分且没有疑似编造的引用即为通过，通过后才能申请投稿。',
          'A review passes with a rating of 6 or higher and no suspect citations. You can request submission once it passes.',
        )}
      </div>
    </Modal>
  );
}

/* ---------------- 总览卡 ---------------- */

function DimBar({ zh, en, value }: { zh: string; en: string; value: number | null | undefined }) {
  const v = typeof value === 'number' ? value : null;
  const label = tr(zh, en);
  return (
    <div className="row gap10" style={{ marginBottom: 10 }}>
      <span style={{ width: 96, flexShrink: 0, fontSize: 12, color: 'var(--text-2)' }}>
        {label}
      </span>
      <div className="row" style={{ flex: 1, gap: 4 }} title={`${label}: ${v ?? '—'} / 4`}>
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
      <span className="mono" style={{ width: 44, textAlign: 'right', fontWeight: 600, fontSize: 13 }}>
        {v ?? '—'}
        <span style={{ color: 'var(--text-3)', fontWeight: 400 }}> /4</span>
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
          {tr('评审结论', 'Summary')}
        </span>
        {guardrail?.passed === false && (
          <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr('部分审稿意见不可靠，未计入总评', 'Some reviews were unreliable and left out of the rating')}
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
          <div className="mono" style={{ fontSize: 42, fontWeight: 600, lineHeight: 1, color: 'var(--accent-text)' }}>
            {rating != null ? rating : '—'}
            <span style={{ fontSize: 15, color: 'var(--text-3)', fontWeight: 500 }}> /10</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr('总评', 'Overall rating')}
          </div>
          {decision && (
            <StatusText tone={decision.tone}>{tr(decision.zh, decision.en)}</StatusText>
          )}
        </div>
        {/* 三维度条形 1-4 */}
        <div style={{ flex: 1, minWidth: 280, alignSelf: 'center' }}>
          {META_DIMS.map((d) => (
            <DimBar key={d.key} zh={d.zh} en={d.en} value={meta?.[d.key]} />
          ))}
          <div className="row gap8" style={{ marginTop: 4, flexWrap: 'wrap' }}>
            {ratings.length > 0 && (
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {tr(
                  `各审稿人评分：${ratings.join(' / ')}，取中位数`,
                  `Reviewer ratings: ${ratings.join(' / ')} (median used)`,
                )}
              </span>
            )}
            {fabricated > 0 && (
              <StatusText tone="danger">
                {tr(`${fabricated} 条引用疑似编造，本轮不通过`, `${fabricated} suspect ${fabricated === 1 ? 'citation' : 'citations'}, so this review fails`)}
              </StatusText>
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
        <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{zh}</span>
        <span className="mono" style={{ fontSize: 11, fontWeight: 600 }}>
          {v ?? '—'}<span style={{ color: 'var(--text-3)', fontWeight: 400 }}>/{max}</span>
        </span>
      </div>
      <div className="bar" style={{ height: 5 }}>
        <i style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function OpinionZone({ title, items, tx }: { title: string; items: string[] | undefined; tx: string }) {
  if (!items || items.length === 0) return null;
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: tx, marginBottom: 4 }}>{title}</div>
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
        <span style={{ fontSize: 13, fontWeight: 600 }}>{msg.author_name}</span>
        {op && typeof op.confidence === 'number' && (
          <span
            className="mono"
            style={{ fontSize: 11, color: 'var(--text-3)' }}
            title={tr('审稿人对自己判断的信心', 'How sure the reviewer is of this review')}
          >
            {tr('信心', 'Confidence')} {op.confidence}/5
          </span>
        )}
        {unreliable && (
          <span
            style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 'auto' }}
            title={tr('内容不够具体，或与论文不符', 'Too vague, or doesn’t match the paper')}
          >
            {tr('不可靠，未计入总评', 'Unreliable, not counted')}
          </span>
        )}
      </div>
      {op ? (
        <>
          <div className="row gap10" style={{ marginBottom: 4 }}>
            <MiniScale zh={tr('严谨', 'Soundness')} value={op.soundness} max={4} />
            <MiniScale zh={tr('表达', 'Presentation')} value={op.presentation} max={4} />
            <MiniScale zh={tr('贡献', 'Contribution')} value={op.contribution} max={4} />
            <MiniScale zh={tr('总评', 'Rating')} value={op.rating} max={10} />
          </div>
          <OpinionZone title={tr('优点', 'Strengths')} items={op.strengths} tx="var(--ok-tx)" />
          <OpinionZone title={tr('缺点', 'Weaknesses')} items={op.weaknesses} tx="var(--danger-tx)" />
          <OpinionZone title={tr('提问', 'Questions')} items={op.questions} tx="var(--warn-tx)" />
        </>
      ) : (
        <Markdown source={msg.content} style={{ fontSize: 13 }} />
      )}
    </div>
  );
}

/* ---------------- 引用核验表 ---------------- */

const CIT_GRID = 'minmax(110px, 170px) 92px 150px 92px minmax(0, 1fr)';

function CitationRow({ item }: { item: CitationCheckItem }) {
  const ex = EXISTENCE_META[item.existence] ?? { zh: item.existence, bg: 'var(--surface-3)', tx: 'var(--text-3)' };
  const sp = item.support ? SUPPORT_META[item.support] : null;
  const src = SOURCE_TEXT[item.source ?? ''];
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
      title={item.context_snippet ? `${tr('引用上下文：', 'Context: ')}${item.context_snippet}` : undefined}
    >
      <span className="mono" style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {item.bibkey}
      </span>
      <span className="pill sm" style={{ background: ex.bg, color: ex.tx, justifySelf: 'start' }}>{tr(ex.zh, ex.en)}</span>
      <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{src ? tr(src.zh, src.en) : item.source ?? '—'}</span>
      {sp ? (
        <span className="pill sm" style={{ background: sp.bg, color: sp.tx, justifySelf: 'start' }}>{tr(sp.zh, sp.en)}</span>
      ) : (
        <span className="muted mono" style={{ fontSize: 11 }}>—</span>
      )}
      <span
        style={{
          fontSize: 12,
          color: 'var(--text-2)',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {item.matched_title ?? <span className="muted">{tr('未匹配到论文', 'No match')}</span>}
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
          {tr('引用核验', 'Citation check')}
        </span>
        <span className="row gap8" style={{ marginLeft: 'auto', flexWrap: 'wrap' }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr(`${total} 条引用`, `${total} ${total === 1 ? 'citation' : 'citations'}`)}
          </span>
          {fabricated > 0 ? (
            <StatusText tone="danger">
              {tr(`${fabricated} 条疑似编造`, `${fabricated} suspect`)}
            </StatusText>
          ) : (
            items.length > 0 && (
              <StatusText tone="ok">
                {tr('没有疑似编造', 'None suspect')}
              </StatusText>
            )
          )}
          {unsupported > 0 && (
            <StatusText tone="warn">
              {tr(`${unsupported} 条不支撑论点`, `${unsupported} unsupported`)}
            </StatusText>
          )}
        </span>
      </div>
      {items.length === 0 ? (
        <div className="empty" style={{ padding: 24, fontSize: 12 }}>
          {tr('本轮没有引用核验结果。', 'No citation check results for this round.')}
        </div>
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
                fontSize: 11,
                fontWeight: 600,
                color: 'var(--text-3)',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
              }}
            >
              <span>{tr('引用键', 'Bib key')}</span>
              <span>{tr('是否存在', 'Exists')}</span>
              <span>{tr('来源', 'Source')}</span>
              <span>{tr('是否支撑', 'Support')}</span>
              <span>{tr('匹配到的论文', 'Matched paper')}</span>
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
        title={hasEvidence ? tr('展开或收起依据', 'Show or hide evidence') : undefined}
      >
        <span
          title={major ? tr('严重：必须修改', 'Major: must fix') : tr('轻微：建议修改', 'Minor: worth fixing')}
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
            fontSize: 11,
            fontWeight: 600,
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
                  title={tr('在编辑器中打开', 'Open in editor')}
                  style={{ fontSize: 11, color: 'var(--accent-text)' }}
                >
                  {loc} ↗
                </Link>
              ) : (
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{loc}</span>
              ))}
            {item.kind && (
              <span className="pill sm" style={{ height: 16, fontSize: 11, padding: '0 6px' }}>
                {KIND_TEXT[item.kind] ? tr(KIND_TEXT[item.kind]!.zh, KIND_TEXT[item.kind]!.en) : item.kind}
              </span>
            )}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.55, marginTop: 2, overflowWrap: 'break-word' }}>
            {item.issue ?? tr('未说明', 'No description')}
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
              {tr('依据', 'Evidence')}：{item.evidence}
            </div>
          )}
        </div>
        {hasEvidence && (
          <Icon
            name="chevDown"
            size={13}
            style={{ color: 'var(--text-3)', flexShrink: 0, marginTop: 3, transform: open ? 'rotate(180deg)' : 'none' }}
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
          {tr('查错清单', 'Fact check')}
        </span>
        <span className="row gap8" style={{ marginLeft: 'auto' }}>
          {major > 0 && (
            <StatusText tone="danger">
              {tr(`${major} 个严重问题`, `${major} major`)}
            </StatusText>
          )}
          {minor > 0 && (
            <StatusText tone="warn">
              {tr(`${minor} 个轻微问题`, `${minor} minor`)}
            </StatusText>
          )}
          {items.length === 0 && (
            <StatusText tone="ok">
              {tr('没有发现问题', 'No issues found')}
            </StatusText>
          )}
        </span>
      </div>
      {items.length === 0 ? (
        <div className="empty" style={{ padding: 24, fontSize: 12 }}>
          {tr('数字、论述和图表引用均已核对。', 'Numbers, claims and figure references all check out.')}
        </div>
      ) : (
        <div style={{ borderTop: '0.5px solid var(--border)' }}>
          {items.map((it, i) => (
            // 内容指纹作 key：结果刷新/重排时展开状态不串行（无稳定 id 可用）
            <FactRow key={`${it.location ?? ''}|${it.kind ?? ''}|${it.issue ?? i}`} item={it} msId={msId} files={files} />
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
    onError: (e) => toast(`${tr('发送失败：', 'Couldn’t send: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          {tr('讨论区', 'Discussion')}
        </span>
        {messages.length > 0 && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr(`${messages.length} 条`, `${messages.length} ${messages.length === 1 ? 'message' : 'messages'}`)}
          </span>
        )}
      </div>
      <div style={{ margin: '-6px 22px 12px', fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
        {tr('修订和下一轮评审时会参考这里的评论。', 'Your comments here inform revisions and the next review.')}
      </div>
      <div ref={listRef} className="scroll" style={{ maxHeight: 340, overflowY: 'auto', padding: '4px 22px 8px' }}>
        {messagesQuery.isLoading ? (
          <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
        ) : messagesQuery.isError ? (
          <div className="empty" style={{ padding: 24 }}>
            {tr('无法加载讨论，请确认本机引擎正在运行', 'Couldn’t load the discussion. Make sure the local engine is running.')}
          </div>
        ) : messages.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>
            {tr('还没有评论', 'No comments yet')}
          </div>
        ) : (
          messages.map((m) => <DiscussionBubble key={m.id} msg={m} />)
        )}
      </div>
      <div className="row gap10" style={{ padding: '12px 22px 18px', borderTop: '0.5px solid var(--border)' }}>
        <textarea
          className="textarea"
          rows={2}
          placeholder={tr('写评论，Enter 发送，Shift Enter 换行', 'Write a comment. Enter to send, Shift Enter for a new line')}
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
          {tr('发送', 'Send')}
        </button>
      </div>
    </div>
  );
}

/* ---------------- 页面 ---------------- */

/** 评审意见（总览 + 逐评审员 + 讨论） / 核对（引用核验 + 查错清单） */
type ReviewTab = 'opinions' | 'checks';

export function PaperReviewPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const [msId, setMsId] = useState<string | null>(null);
  const [roundSid, setRoundSid] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState<ReviewTab>('opinions');

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
      toast(tr('已提交投稿审批', 'Sent for submission approval'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', msId] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        if (e.message.includes('REVIEW_REQUIRED')) {
          toast(tr('请先通过同行评审再投稿', 'Pass peer review before submitting'), 'error');
        } else if (e.message.includes('COMPILE_REQUIRED')) {
          toast(tr('请先成功编译稿件，可在编辑器中按 ⌘S', 'Compile the manuscript first. Press ⌘S in the editor.'), 'error');
        } else {
          toast(`${tr('无法投稿：', 'Couldn’t submit: ')}${e.message}`, 'error');
        }
      } else {
        toast(`${tr('无法投稿：', 'Couldn’t submit: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  function onRevise() {
    if (!msId) return;
    if (!reviewPassed) {
      toast(tr('修改建议已在事实包中', 'Suggested fixes are in the fact pack'), 'info');
    }
    navigate(`/writer/${msId}`);
  }

  /* ---------------- 渲染 ---------------- */

  const noManuscripts = !manuscriptsQuery.isLoading && !manuscriptsQuery.isError && reviewable.length === 0;

  return (
    <div className="page fadeup" style={{ maxWidth: 1280 }}>
      <PageHead
        eyebrow="Stage 05 · Paper Review"
        title={tr('论文评审', 'Paper Review')}
        sub={
          currentProject
            ? undefined
            : projectsLoading
              ? tr('加载中…', 'Loading…')
              : tr('请先选择课题', 'Choose a topic first')
        }
      />

      {/* —— 顶部一行：两个视图标签在左，发起评审在右 —— */}
      <div className="row page-tabs" style={{ marginBottom: 14 }}>
        <Segmented<ReviewTab>
          options={[
            { v: 'opinions', label: tr('审稿意见', 'Reviews') },
            { v: 'checks', label: tr('核对', 'Checks') },
          ]}
          value={tab}
          onChange={setTab}
        />
        <div style={{ marginLeft: 'auto' }}>
          <button
            className={`btn sm ${reviewPassed ? 'btn-soft' : 'btn-primary'}`}
            disabled={!msId || !!runningReview}
            title={
              !msId
                ? tr('请先选择稿件', 'Choose a manuscript first')
                : runningReview
                  ? tr('评审正在进行', 'A review is in progress')
                  : tr('开始新一轮同行评审', 'Start a new round of peer review')
            }
            onClick={() => setModalOpen(true)}
          >
            {runningReview ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('评审中…', 'Reviewing…')}
              </>
            ) : (
              <>
                <Icon name="shield" size={13} />
                {tr('发起同行评审', 'Start peer review')}
              </>
            )}
          </button>
        </div>
      </div>

      {/* —— 稿件 + 评审轮次选择 —— */}
      <div className="card card-pad row gap10" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-2)', flexShrink: 0 }}>
          {tr('稿件', 'Manuscript')}
        </span>
        <select
          className="input"
          style={{ flex: '0 1 420px', minWidth: 0 }}
          value={msId ?? ''}
          disabled={reviewable.length === 0}
          onChange={(e) => {
            setMsId(e.target.value);
            setRoundSid(null);
          }}
        >
          <option value="" disabled>
            {manuscriptsQuery.isLoading
              ? tr('加载中…', 'Loading…')
              : manuscriptsQuery.isError
                ? tr('无法加载稿件', 'Couldn’t load manuscripts')
                : tr('选择稿件', 'Choose a manuscript')}
          </option>
          {reviewable.map((m) => (
            <option key={m.id} value={m.id}>
              {tr(
                `${m.title}（${m.status === 'compiled' ? '已编译' : '评审中'}）`,
                `${m.title} (${m.status === 'compiled' ? 'compiled' : 'under review'})`,
              )}
            </option>
          ))}
        </select>
        {msId && (
          <Link to={`/writer/${msId}`} style={{ fontSize: 12, color: 'var(--accent-text)', whiteSpace: 'nowrap' }}>
            {tr('在编辑器中打开', 'Open in editor')}
          </Link>
        )}
        {reviews.length > 0 && (
          <span className="row gap8" style={{ marginLeft: 'auto' }}>
            <span style={{ fontSize: 12, color: 'var(--text-3)', flexShrink: 0 }}>{tr('轮次', 'Round')}</span>
            <select
              className="input"
              style={{ width: 220 }}
              value={selected?.session_id ?? ''}
              onChange={(e) => setRoundSid(e.target.value)}
            >
              {reviews.map((r, i) => (
                <option key={r.session_id} value={r.session_id}>
                  {tr(`第 ${reviews.length - i} 轮`, `Round ${reviews.length - i}`)} · {fmtTime(r.created_at)}
                  {i === 0 ? tr('（最新）', ' (latest)') : ''}
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
            <span className="pulse" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)', flexShrink: 0 }} />
            <span style={{ fontSize: 13, fontWeight: 600 }}>
              {tr('同行评审进行中', 'Peer review in progress')}
            </span>
            <span style={{ fontSize: 12, color: 'var(--accent-text)', marginLeft: 'auto' }}>
              {tr('查看进度', 'View progress')}
            </span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      {/* —— 主体 —— */}
      {!pid ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? tr('加载中…', 'Loading…') : tr('请先选择课题', 'Choose a topic first')}
          </div>
        </div>
      ) : noManuscripts ? (
        <div className="card">
          <EmptyState
            icon="pen"
            title={tr('还没有可评审的稿件', 'No manuscripts to review yet')}
            desc={tr('稿件编译成功后即可评审。', 'A manuscript can be reviewed once it compiles.')}
            action={
              <button className="btn btn-ghost" onClick={() => navigate(topicPath(pid, 'writer'))}>
                <Icon name="pen" size={14} />
                {tr('前往论文撰写', 'Open Paper Writer')}
              </button>
            }
          />
        </div>
      ) : manuscriptsQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载稿件', 'Couldn’t load manuscripts')}
            desc={tr('请确认本机引擎正在运行，然后重试。', 'Make sure the local engine is running, then retry.')}
            action={
              <button className="btn btn-soft sm" onClick={() => void manuscriptsQuery.refetch()}>{tr('重试', 'Retry')}</button>
            }
          />
        </div>
      ) : !msId ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{tr('加载中…', 'Loading…')}</div>
        </div>
      ) : reviewsQuery.isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{tr('加载中…', 'Loading…')}</div>
        </div>
      ) : reviewsQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载评审记录', 'Couldn’t load reviews')}
            desc={tr('请确认本机引擎正在运行，然后重试。', 'Make sure the local engine is running, then retry.')}
            action={<button className="btn btn-soft sm" onClick={() => void reviewsQuery.refetch()}>{tr('重试', 'Retry')}</button>}
          />
        </div>
      ) : !selected ? (
        <div className="card">
          <EmptyState
            icon="shield"
            title={tr('这篇稿件还没有评审', 'This manuscript hasn’t been reviewed yet')}
            desc={tr(
              'AI 审稿人会核对引用和数字、打分，并给出接收或拒稿建议。',
              'AI reviewers check citations and numbers, score the paper and suggest accept or reject.',
            )}
            action={
              <button className="btn btn-soft" disabled={!!runningReview} onClick={() => setModalOpen(true)}>
                <Icon name="shield" size={14} />
                {tr('发起同行评审', 'Start peer review')}
              </button>
            }
          />
        </div>
      ) : (
        <>
          {tab === 'opinions' && (
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
                {tr('审稿意见', 'Reviews')}
              </span>
              {roundNo > 0 && (
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {tr(`第 ${roundNo} 轮`, `Round ${roundNo}`)} · {fmtTime(selected.created_at)}
                </span>
              )}
            </div>
            {messagesQuery.isLoading ? (
              <div className="card">
                <div className="empty" style={{ padding: 30 }}>{tr('加载中…', 'Loading…')}</div>
              </div>
            ) : messagesQuery.isError ? (
              <div className="card">
                <div className="empty" style={{ padding: 30 }}>
                  {tr('无法加载审稿意见，请确认本机引擎正在运行', 'Couldn’t load reviews. Make sure the local engine is running.')}
                </div>
              </div>
            ) : reviewerCards.length === 0 ? (
              <div className="card">
                <div className="empty" style={{ padding: 30 }}>{tr('本轮还没有审稿意见。', 'No reviews in this round yet.')}</div>
              </div>
            ) : (
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(min(300px, 100%), 1fr))',
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

          {/* 人类讨论 */}
          {sid && <ReviewDiscussion sessionId={sid} />}
          </>
          )}

          {tab === 'checks' && (
          <>
            {/* 引用核验 */}
            <CitationCard check={citation} />

            {/* 查错清单 */}
            <FactCheckCard items={factItems} msId={msId} files={detail?.files} />
          </>
          )}

          {/* 底部操作 */}
          <div className="card card-pad row gap10" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
            <div className="row gap8" style={{ fontSize: 13, color: 'var(--text-2)', minWidth: 0 }}>
              {reviewPassed ? (
                <>
                  <StatusText tone="ok">
                    {tr('评审已通过', 'Review passed')}
                  </StatusText>
                  <span>{tr('可以申请投稿，或继续修订。', 'Request submission, or keep revising.')}</span>
                </>
              ) : (
                <>
                  <StatusText tone="warn">
                    {tr('评审未通过', 'Review didn’t pass')}
                  </StatusText>
                  <span>
                    {tr(
                      '修改建议已加入事实包，修订后可再评审一轮。',
                      'Suggested fixes were added to the fact pack. Revise, then review again.',
                    )}
                  </span>
                </>
              )}
            </div>
            <div className="row gap10">
              <button className="btn btn-ghost" onClick={onRevise}>
                <Icon name="pen" size={14} />
                {tr('去修订', 'Revise')}
              </button>
              <button
                className={`btn ${reviewPassed ? 'btn-primary' : 'btn-soft'}`}
                disabled={!reviewPassed || submitMutation.isPending}
                title={
                  reviewPassed
                    ? tr('提交投稿审批', 'Request submission approval')
                    : tr('通过同行评审后才能投稿', 'Pass peer review before submitting')
                }
                onClick={() => submitMutation.mutate()}
              >
                <Icon name="arrow" size={14} />
                {submitMutation.isPending ? tr('提交中…', 'Submitting…') : tr('申请投稿', 'Request submission')}
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
