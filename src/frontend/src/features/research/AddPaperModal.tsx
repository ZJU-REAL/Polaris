import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, type ShelfImportInput } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { parsePaperRef } from '../../lib/paper-ref';
import { SearchInput, useDebounced } from '../wiki/shared';

/* ============================================================
   相关研究 · 添加文献统一入口（弹窗，两个页签）：
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
  { v: 'manual', zh: '按编号添加', en: 'By ID' },
];

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
    const input: ShelfImportInput | null = parsePaperRef(importInput);
    if (!input) {
      toast(
        importInput.trim()
          ? tr('无法识别这个编号，请输入 arXiv 编号、DOI 或 PMID', 'That isn’t an arXiv ID, DOI or PMID')
          : tr('先输入 arXiv 编号、DOI 或 PMID', 'Enter an arXiv ID, DOI or PMID first'),
        'info',
      );
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
      title={tr('添加论文', 'Add paper')}
    >
      <Segmented<AddTab> options={TABS.map((t) => ({ v: t.v, label: tr(t.zh, t.en) }))} value={tab} onChange={setTab} />

      {tab === 'library' ? (
        /* ======== 从文献库检索 ======== */
        <div style={{ marginTop: 14 }}>
          <div className="row gap10">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={tr('搜索标题、摘要或解读…', 'Search titles, abstracts or summaries…')}
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
              {tr('输入关键词，搜索课题关联的文献库', 'Type a keyword to search this topic’s libraries')}
            </div>
          ) : searchQuery.isLoading ? (
            <div className="empty" style={{ padding: '28px 14px' }}>{tr('搜索中…', 'Searching…')}</div>
          ) : results.length === 0 ? (
            <div className="empty" style={{ padding: '28px 14px' }}>
              {tr('没有找到，可以切换到「按编号添加」', 'Nothing found. Try adding it by ID.')}
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
                      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
                      <div className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                        {p.arxiv_id ?? p.venue ?? '—'}
                        {p.year !== null ? ` · ${p.year}` : ''}
                      </div>
                    </div>
                    {added ? (
                      <span className="row gap6" style={{ fontSize: 12, color: 'var(--ok-tx)', flexShrink: 0 }}>
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
        /* ======== 手动添加：arXiv / DOI / PMID ======== */
        <div style={{ marginTop: 14 }}>
          <div className="row gap10">
            <input
              className="input"
              autoFocus
              style={{ height: 32, fontSize: 13, flex: 1, minWidth: 0 }}
              value={importInput}
              onChange={(e) => setImportInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitImport();
              }}
              placeholder={tr(
                '例如 2401.12345、10.1234/abc、31452104 或论文链接',
                'e.g. 2401.12345, 10.1234/abc, 31452104 or a paper link',
              )}
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
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 10, lineHeight: 1.6 }}>
            {tr(
              '支持 arXiv 编号、DOI 和 PMID。论文会同时加入我的文献库。',
              'Accepts arXiv IDs, DOIs and PMIDs. The paper is also added to My library.',
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
