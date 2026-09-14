import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { api, type MethodCard, type MethodSearchMode } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   方法库（#663）：库内论文的「做法」视图。
   每张卡是一篇论文的方法五元组（目的 / 机制 / 基线 / 数据集 / 流程），
   由后台从全文自动抽取（method@1 schema）。搜索走 purpose–mechanism
   双轴向量：「找同类做法」= 目的相近；「找异类机制」= 目的相近但
   换了路子的做法——类比检索，找灵感用。
   ============================================================ */

/** 卡片里的一段字段：无内容不渲染（后端约定：没抽到就缺键）。 */
/** 内置口径的五项：界面对它们有自己的中英文案，不走包提供的 label。 */
const BUILTIN_FIELD_NAMES = new Set(['purpose', 'mechanism', 'baseline', 'dataset', 'protocol']);

/** 包字段的值可能是文本、字符串数组，或结构化条目（如「指标/数值/条件」三元组）。 */
function renderFieldValue(value: unknown): ReactNode {
  if (value === null || value === undefined) return null;
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    if (typeof value[0] === 'string') return (value as string[]).join(tr('；', '; '));
    // entries：每条是白名单键的对象，按键序拼成一行，键名不再重复展示
    return (value as Record<string, unknown>[])
      .map((entry) =>
        Object.values(entry)
          .filter((v) => v !== null && v !== undefined && v !== '')
          .join(' · '),
      )
      .join(tr('；', '; '));
  }
  return String(value);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ fontSize: 12, lineHeight: 1.6 }}>
      <span style={{ color: 'var(--text-4)', fontWeight: 650, marginRight: 6 }}>{label}</span>
      <span style={{ color: 'var(--text-2)' }}>{children}</span>
    </div>
  );
}

function MethodCardView({
  card,
  onOpenPaper,
}: {
  card: MethodCard;
  onOpenPaper: (id: string) => void;
}) {
  return (
    <div
      className="card"
      style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 8 }}
    >
      <div className="row gap8" style={{ minWidth: 0 }}>
        <span
          style={{
            fontSize: 13,
            fontWeight: 650,
            cursor: 'pointer',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
            flex: 1,
          }}
          title={`${card.title} · ${tr('点击打开论文', 'click to open paper')}`}
          onClick={() => onOpenPaper(card.paper_id)}
        >
          {card.title}
        </span>
        {typeof card.similarity === 'number' && (
          <span
            className="mono"
            style={{ fontSize: 10, color: 'var(--text-4)', flexShrink: 0 }}
            title={tr('目的相似度', 'purpose similarity')}
          >
            {card.similarity.toFixed(2)}
          </span>
        )}
      </div>
      {card.purpose && <Field label={tr('目的', 'Purpose')}>{card.purpose}</Field>}
      {card.mechanism && <Field label={tr('机制', 'Mechanism')}>{card.mechanism}</Field>}
      {card.baseline.length > 0 && (
        <Field label={tr('基线', 'Baselines')}>{card.baseline.join(tr('；', '; '))}</Field>
      )}
      {card.dataset.length > 0 && (
        <Field label={tr('数据集', 'Datasets')}>{card.dataset.join(tr('；', '; '))}</Field>
      )}
      {card.protocol && <Field label={tr('流程', 'Protocol')}>{card.protocol}</Field>}
      {/* 学科包换上的字段。上面五项是内置口径、界面有自己的中英文案，所以这里
          只补它们之外的——写死五项的话，结构工程的 构件/作用/分析/验证 一个都不显示 */}
      {(card.fields ?? [])
        .filter((f) => !BUILTIN_FIELD_NAMES.has(f.name))
        .map((f) => (
          <Field key={f.name} label={f.label || f.name}>
            {renderFieldValue(f.value)}
          </Field>
        ))}
    </div>
  );
}

export function MethodsTab({
  libraryId,
  onOpenPaper,
}: {
  libraryId: string;
  onOpenPaper: (id: string) => void;
}) {
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<MethodSearchMode>('same_purpose');

  const listQuery = useQuery({
    queryKey: ['library-methods', libraryId],
    queryFn: () => api.listLibraryMethods(libraryId),
    enabled: !query,
    retry: false,
  });
  const searchQuery = useQuery({
    queryKey: ['library-methods-search', libraryId, query, mode],
    queryFn: () => api.searchLibraryMethods(libraryId, { q: query, mode }),
    enabled: !!query,
    retry: false,
  });

  const searching = !!query;
  const cards = searching ? (searchQuery.data?.items ?? []) : (listQuery.data ?? []);
  const loading = searching ? searchQuery.isLoading : listQuery.isLoading;
  const error = searching ? searchQuery.isError : listQuery.isError;

  const submit = () => setQuery(input.trim());

  return (
    <div className="col" style={{ flex: 1, minHeight: 0, padding: 16, gap: 12, overflowY: 'auto' }}>
      <div className="row gap8" style={{ alignItems: 'stretch', flexWrap: 'wrap' }}>
        <input
          className="input"
          style={{ flex: 1, minWidth: 220 }}
          value={input}
          placeholder={tr(
            '描述你想达成的目标，找库里论文的做法…',
            'Describe the goal; find how papers in this library approach it…',
          )}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit();
          }}
        />
        <Segmented<MethodSearchMode>
          options={[
            { v: 'same_purpose', label: tr('找同类做法', 'Similar approaches') },
            { v: 'different_mechanism', label: tr('找异类机制', 'Different mechanisms') },
          ]}
          value={mode}
          onChange={setMode}
        />
        <button className="btn btn-primary sm" disabled={!input.trim()} onClick={submit}>
          {tr('搜方法', 'Search')}
        </button>
        {searching && (
          <button
            className="btn btn-ghost sm"
            onClick={() => {
              setQuery('');
              setInput('');
            }}
          >
            {tr('清空', 'Clear')}
          </button>
        )}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-4)' }}>
        {mode === 'same_purpose'
          ? tr(
              '找同类做法：按「要达成什么」找目的相近的论文。',
              'Similar approaches: papers whose purpose is closest to your goal.',
            )
          : tr(
              '找异类机制：目的和你相近、但用了不同思路的论文排在前面——找灵感用。',
              'Different mechanisms: papers with a similar purpose but a different approach come first.',
            )}
        {searching && searchQuery.data?.mode_used === 'keyword' && (
          <span style={{ marginLeft: 6 }}>
            {tr('（语义检索暂不可用，已按关键词匹配）', '(semantic search unavailable; matched by keywords)')}
          </span>
        )}
      </div>

      {error && (
        <div className="card" style={{ padding: 12, color: 'var(--danger-tx, #c00)', fontSize: 12 }}>
          {tr('加载方法库失败（后端不可用）', 'Failed to load the method library (backend unavailable)')}
        </div>
      )}
      {loading && <div className="skel" style={{ height: 120 }} />}

      {!loading && !error && cards.length === 0 && (
        <EmptyState
          icon="layers"
          title={
            searching
              ? tr('没有匹配的方法卡', 'No matching method cards')
              : tr('还没有方法卡', 'No method cards yet')
          }
          desc={
            searching
              ? tr('换个说法描述目标试试。', 'Try describing the goal differently.')
              : tr(
                  '论文全文就位后会自动抽出方法卡（目的 / 机制 / 基线 / 数据集 / 流程）。',
                  'Method cards (purpose / mechanism / baselines / datasets / protocol) are extracted automatically once full text is available.',
                )
          }
        />
      )}

      {cards.length > 0 && (
        <div className="col" style={{ gap: 10 }}>
          {!searching && (
            <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
              {tr(`方法卡 · ${cards.length} 张`, `Method cards · ${cards.length}`)}
            </span>
          )}
          {cards.map((card) => (
            <MethodCardView key={card.paper_id} card={card} onOpenPaper={onOpenPaper} />
          ))}
        </div>
      )}
    </div>
  );
}
