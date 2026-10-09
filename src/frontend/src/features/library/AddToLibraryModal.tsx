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

type AddMethod = 'ref' | 'corpus' | 'bibtex';

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
  const [corpusId, setCorpusId] = useState('');
  const [bibtex, setBibtex] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);
  // 后端返回 task_id 时弹出分阶段处理进度（下载 / 抽取 / 向量化）
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);

  const reset = () => {
    setRef('');
    setCorpusId('');
    setBibtex('');
    setParseError(null);
  };

  const input: PaperImportInput | null =
    method === 'bibtex'
      ? (bibtex.trim() ? { bibtex: bibtex.trim() } : null)
      : method === 'corpus'
        ? (corpusId.trim() ? { corpus_id: corpusId.trim() } : null)
        : parsePaperRef(ref);

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
        toast(tr('已添加到我的收藏', 'Added to Saved'), 'ok');
      }
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        setParseError(
          tr('无法解析：', 'Couldn’t parse: ') +
            (e.message.replace(/^PARSE_FAILED:?\s*/, '') || tr('请检查格式', 'check the format')),
        );
      } else if (e instanceof ApiError && e.status === 503) {
        // 上游（arXiv/OpenAlex）在限流。说清楚是别人家的问题、以及能怎么办——
        // 「添加失败」三个字会让用户以为是自己输错了编号。
        setParseError(
          tr(
            'arXiv 暂时繁忙，请几分钟后重试，或改用 DOI、BibTeX 添加。',
            'arXiv is busy right now. Try again in a few minutes, or add the paper by DOI or BibTeX.',
          ),
        );
      } else {
        toast(`${tr('添加失败：', 'Couldn’t add: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        width={520}
        title={tr('添加论文', 'Add paper')}
        sub={tr('添加到我的收藏。', 'Adds the paper to Saved.')}
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
            { v: 'ref', label: tr('编号或链接', 'ID or link') },
            { v: 'corpus', label: 'Corpus ID' },
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
              <div className="muted" style={{ fontSize: 12, marginTop: 8, lineHeight: 1.6 }}>
                {tr(
                  '支持 arXiv 编号、DOI、PMID 或论文链接。',
                  'Accepts an arXiv ID, DOI, PMID or paper link.',
                )}
              </div>
            </>
          ) : method === 'corpus' ? (
            <>
              <input
                className="input mono"
                autoFocus
                style={{ width: '100%' }}
                placeholder={tr('例如 13756489', 'e.g. 13756489')}
                value={corpusId}
                onChange={(e) => {
                  setCorpusId(e.target.value);
                  setParseError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && input && !importMutation.isPending) {
                    importMutation.mutate(input);
                  }
                }}
              />
              <div className="muted" style={{ fontSize: 12, marginTop: 8, lineHeight: 1.6 }}>
                {tr('Semantic Scholar 的论文编号。', 'The paper’s Semantic Scholar ID.')}
              </div>
            </>
          ) : (
            <>
              <textarea
                className="textarea mono"
                autoFocus
                style={{ width: '100%', minHeight: 150, resize: 'vertical', fontSize: 12 }}
                placeholder={'@article{smith2024,\n  title = {…},\n  author = {…},\n  year = {2024}\n}'}
                value={bibtex}
                onChange={(e) => {
                  setBibtex(e.target.value);
                  setParseError(null);
                }}
              />
              <div className="muted" style={{ fontSize: 12, marginTop: 8, lineHeight: 1.6 }}>
                {tr('每次一条，须包含 title。', 'One entry at a time. A title is required.')}
              </div>
            </>
          )}
          {parseError && (
            <div
              style={{
                marginTop: 10,
                fontSize: 12,
                color: 'var(--danger-tx)',
                background: 'var(--danger-bg)',
                borderRadius: 8,
                padding: '7px 10px',
                lineHeight: 1.6,
              }}
            >
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
