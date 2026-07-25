import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type LibraryEntry, type PaperImportInput } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { parsePaperRef } from '../../lib/paper-ref';
import { PaperProgressModal } from './PaperProgressModal';

/* ============================================================
   我的文献库 · 添加文献弹窗：
   arXiv 编号 / DOI（一个输入框自动识别，粘链接也行）或 BibTeX。
   平台已有这篇时直接复用，没有才去抓取；添加进来的只进我的收藏，
   不进任何公共文献库。需要下载/抽取时弹分阶段进度。
   ============================================================ */

type AddMethod = 'ref' | 'bibtex';

export function AddToLibraryModal({
  open,
  onClose,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  /** 添加成功：外层刷新列表并选中这一条 */
  onAdded: (entry: LibraryEntry) => void;
}) {
  const queryClient = useQueryClient();
  const [method, setMethod] = useState<AddMethod>('ref');
  const [ref, setRef] = useState('');
  const [bibtex, setBibtex] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);
  // 后端返回 task_id 时弹出分阶段处理进度（下载 / 抽取 / 向量化）
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);

  const reset = () => {
    setRef('');
    setBibtex('');
    setParseError(null);
  };

  const input: PaperImportInput | null =
    method === 'bibtex' ? (bibtex.trim() ? { bibtex: bibtex.trim() } : null) : parsePaperRef(ref);

  const importMutation = useMutation({
    mutationFn: (inp: PaperImportInput) => api.importToLibrary(inp),
    onSuccess: (entry) => {
      void queryClient.invalidateQueries({ queryKey: ['library'] });
      void queryClient.invalidateQueries({ queryKey: ['library-state'] });
      reset();
      onClose();
      onAdded(entry);
      if (entry.task_id) {
        // 还要下载正文 → 弹进度替代成功 toast，避免重复打扰
        setProgress({ taskId: entry.task_id, title: entry.title });
      } else {
        toast(tr('已加进我的收藏', 'Added to your saved papers'), 'ok');
      }
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        setParseError(
          e.message.replace(/^PARSE_FAILED:?\s*/, '') ||
            tr('内容解析失败，请检查格式', 'Failed to parse — check the format'),
        );
      } else {
        toast(`${tr('添加失败：', 'Failed to add: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        width={520}
        title={tr('添加文献', 'Add paper')}
        sub={tr(
          '加进我的收藏，只有你自己看得到。',
          'Goes into your saved papers — visible only to you.',
        )}
        footer={
          <>
            <button className="btn btn-ghost sm" onClick={onClose}>
              {tr('取消', 'Cancel')}
            </button>
            <button
              className="btn btn-primary sm"
              disabled={!input || importMutation.isPending}
              onClick={() => input && importMutation.mutate(input)}
            >
              {importMutation.isPending ? (
                <>
                  <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('添加中…', 'Adding…')}
                </>
              ) : (
                <>
                  <Icon name="plus" size={13} />
                  {tr('添加', 'Add')}
                </>
              )}
            </button>
          </>
        }
      >
        <Segmented<AddMethod>
          options={[
            { v: 'ref', label: tr('arXiv 编号 / DOI', 'arXiv ID / DOI') },
            { v: 'bibtex', label: tr('粘贴 BibTeX', 'Paste BibTeX') },
          ]}
          value={method}
          onChange={(m) => {
            setMethod(m);
            setParseError(null);
          }}
        />
        <div style={{ marginTop: 14 }}>
          {method === 'ref' ? (
            <>
              <input
                className="input mono"
                autoFocus
                style={{ width: '100%' }}
                placeholder={tr(
                  '例如 2405.01234 或 10.1145/3567890.1234567',
                  'e.g. 2405.01234 or 10.1145/3567890.1234567',
                )}
                value={ref}
                onChange={(e) => {
                  setRef(e.target.value);
                  setParseError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && input && !importMutation.isPending) {
                    importMutation.mutate(input);
                  }
                }}
              />
              <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
                {tr(
                  '编号或论文链接都行，标题、作者、摘要会自动抓取，有 PDF 的顺带下载。',
                  'An ID or a paper link both work — title, authors and abstract are fetched automatically, plus the PDF when available.',
                )}
              </div>
            </>
          ) : (
            <>
              <textarea
                className="textarea mono"
                autoFocus
                style={{ width: '100%', minHeight: 150, resize: 'vertical', fontSize: 12 }}
                placeholder={tr(
                  '粘贴单条 BibTeX 条目，例如：\n@inproceedings{smith2024example,\n  title = {...},\n  author = {...},\n  year = {2024},\n}',
                  'Paste one BibTeX entry, e.g.:\n@inproceedings{smith2024example,\n  title = {...},\n  author = {...},\n  year = {2024},\n}',
                )}
                value={bibtex}
                onChange={(e) => {
                  setBibtex(e.target.value);
                  setParseError(null);
                }}
              />
              <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
                {tr(
                  '一次粘贴一条；title 必填，作者/年份/期刊/DOI 能解析多少取多少。',
                  'One entry at a time; title is required — authors/year/venue/DOI are parsed on a best-effort basis.',
                )}
              </div>
            </>
          )}
          <div className="muted" style={{ fontSize: 11.5, marginTop: 10, lineHeight: 1.6 }}>
            {tr(
              '平台上已经有这篇的话直接复用，不会重复解析；已经在回收站里的会重新回到收藏。',
              'If the paper is already on the platform it is reused instead of parsed again; one that sits in your trash comes back to your saved papers.',
            )}
          </div>
          {parseError && (
            <div
              style={{
                marginTop: 10,
                fontSize: 11.5,
                color: 'var(--danger-tx)',
                background: 'var(--danger-bg)',
                borderRadius: 8,
                padding: '7px 10px',
                lineHeight: 1.6,
              }}
            >
              {tr('解析失败：', 'Parse failed: ')}
              {parseError}
            </div>
          )}
        </div>
      </Modal>
      {progress && (
        <PaperProgressModal
          taskId={progress.taskId}
          paperTitle={progress.title}
          onClose={() => setProgress(null)}
          onDone={() => {
            void queryClient.invalidateQueries({ queryKey: ['library'] });
          }}
        />
      )}
    </>
  );
}
