import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, type ShelfImportInput } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { SearchInput, useDebounced } from '../wiki/shared';

/* ============================================================
   相关研究 · 「添加论文」统一入口（弹窗，两个页签）：
   - 从文献库：检索当前课题关联的文献库，结果行内一键添加，
     已入架的显示勾选态；找不到时引导切到手动添加。
   - 手动添加：arXiv 编号 / DOI，平台自动查重（池里已有直接
     复用解析，不重复花钱）。
   弹窗保持打开，方便连续添加多篇。
   ============================================================ */

type AddTab = 'library' | 'manual';

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const TABS: { v: AddTab; zh: string; en: string }[] = [
  { v: 'library', zh: '从文献库', en: 'From library' },
  { v: 'manual', zh: '手动添加', en: 'By arXiv / DOI' },
];

/** 手动添加输入解析：DOI（10.xxx / doi.org 链接）之外一律按 arXiv 编号处理。 */
function parseImportInput(raw: string): ShelfImportInput | null {
  let v = raw.trim();
  if (!v) return null;
  if (v.includes('doi.org/')) v = v.slice(v.indexOf('doi.org/') + 'doi.org/'.length);
  if (/^10\.\d{4,}/.test(v)) return { doi: v };
  if (v.includes('arxiv.org/')) {
    const seg = v.split('/').filter(Boolean);
    v = (seg[seg.length - 1] ?? '').replace(/\.pdf$/, '');
  }
  return v ? { arxiv_id: v } : null;
}

export function AddPaperModal({
  open,
  onClose,
  pid,
  shelvedIds,
  libraryHref,
  libraryLabel,
  addPending,
  onAdd,
  importPending,
  onImport,
}: {
  open: boolean;
  onClose: () => void;
  pid: string;
  /** 已入架论文 id 集合（勾选态用） */
  shelvedIds: Set<string>;
  /** 文献库入口路径（1 个关联库→进那个库；多个→课题设置；0 个→全部库列表） */
  libraryHref: string;
  /** 文献库入口按钮文案（随关联库数量变化） */
  libraryLabel: string;
  addPending: boolean;
  onAdd: (paperId: string) => void;
  importPending: boolean;
  /** 手动添加；resolve 后清空输入框 */
  onImport: (input: ShelfImportInput) => Promise<unknown>;
}) {
  const navigate = useNavigate();
  const [tab, setTab] = useState<AddTab>('library');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [importInput, setImportInput] = useState('');

  const searchQuery = useQuery({
    queryKey: ['shelf-search', pid, q],
    queryFn: () => api.searchProject(pid, { q, limit: 8 }),
    enabled: !!pid && open && tab === 'library' && q.length > 0,
    retry: false,
    placeholderData: keepPreviousData,
  });
  const results = searchQuery.data?.papers ?? [];

  const submitImport = () => {
    const input = parseImportInput(importInput);
    if (!input) {
      toast(tr('先输入 arXiv 编号或 DOI', 'Enter an arXiv ID or DOI first'), 'info');
      return;
    }
    void onImport(input)
      .then(() => setImportInput(''))
      .catch(() => undefined); // 报错交给外层 mutation 的 toast
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={600}
      title={tr('添加论文', 'Add papers')}
      sub={tr('加入这个课题的相关研究，可以连续添加多篇。', 'Add to related work for this topic — add several in a row.')}
    >
      <Segmented<AddTab> options={TABS.map((t) => ({ v: t.v, label: tr(t.zh, t.en) }))} value={tab} onChange={setTab} />

      {tab === 'library' ? (
        /* ======== 从文献库检索 ======== */
        <div style={{ marginTop: 14 }}>
          <div className="row gap10">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={tr('搜文献库：标题 / 摘要 / 解读…', 'Search the library: title / abstract / wiki…')}
            />
            <button
              className="btn btn-ghost sm"
              style={{ flexShrink: 0 }}
              onClick={() => {
                onClose();
                navigate(libraryHref);
              }}
            >
              <Icon name="book" size={13} />
              {libraryLabel}
            </button>
          </div>

          {q.length === 0 ? (
            <div className="empty" style={{ padding: '28px 14px' }}>
              {tr('输入关键词，搜这个课题关联的文献库', 'Type a keyword to search this topic’s library')}
            </div>
          ) : searchQuery.isLoading ? (
            <div className="empty" style={{ padding: '28px 14px' }}>{tr('搜索中…', 'Searching…')}</div>
          ) : results.length === 0 ? (
            <div className="empty" style={{ padding: '28px 14px' }}>
              {tr('文献库里没搜到，试试「手动添加」页签', 'Nothing found — try the “By arXiv / DOI” tab')}
            </div>
          ) : (
            <div className="col" style={{ marginTop: 8 }}>
              {results.map((p) => {
                const added = shelvedIds.has(p.id);
                return (
                  <div
                    key={p.id}
                    className="row gap10"
                    style={{ padding: '9px 4px', borderBottom: '0.5px solid var(--border)' }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
                      <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 2 }}>
                        {p.arxiv_id ?? p.venue ?? '—'}
                        {p.year !== null ? ` · ${p.year}` : ''}
                      </div>
                    </div>
                    {added ? (
                      <span className="row gap6" style={{ fontSize: 11.5, color: 'var(--ok-tx)', flexShrink: 0 }}>
                        <Icon name="check" size={13} />
                        {tr('已添加', 'Added')}
                      </span>
                    ) : (
                      <button
                        className="btn btn-soft sm"
                        style={{ flexShrink: 0 }}
                        disabled={addPending}
                        onClick={() => onAdd(p.id)}
                      >
                        <Icon name="plus" size={12} />
                        {tr('添加', 'Add')}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : (
        /* ======== 手动添加：arXiv / DOI ======== */
        <div style={{ marginTop: 14 }}>
          <div className="row gap10">
            <input
              className="input"
              autoFocus
              style={{ height: 32, fontSize: 12.5, flex: 1, minWidth: 0 }}
              value={importInput}
              onChange={(e) => setImportInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitImport();
              }}
              placeholder={tr('arXiv 编号（如 2401.12345）或 DOI（如 10.1234/abc）', 'arXiv ID (e.g. 2401.12345) or DOI (e.g. 10.1234/abc)')}
            />
            <button
              className="btn btn-primary sm"
              style={{ flexShrink: 0 }}
              disabled={importPending}
              onClick={submitImport}
            >
              {importPending ? tr('解析中…', 'Resolving…') : tr('添加', 'Add')}
            </button>
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginTop: 10, lineHeight: 1.6 }}>
            {tr(
              '会自动查重：平台已有这篇时直接复用，不会重复解析。文献库里没有的论文会自动抓取元数据和 PDF，只进这个课题的相关研究和你的个人文献库，不影响公共文献库。',
              'Duplicates are detected automatically — papers already on the platform are reused without re-parsing. New papers are fetched (metadata + PDF) and go to this topic and your personal library only; the shared library is untouched.',
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
