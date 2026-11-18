import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type PaperBatchImportInput } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { PaperBatchProgressModal } from '../library/PaperBatchProgressModal';
import { splitPaperInput } from './paperInput';

/* ---------------- 添加文献 Modal ---------------- */

export type ImportMethod = 'arxiv' | 'doi' | 'pmid' | 'corpus' | 'bibtex' | 'zotero';

/** 按 BibTeX 条目的配对大括号/圆括号切分，保留坏条目交给后端逐项报错。 */
function splitBibtexEntries(raw: string): string[] {
  const entries: string[] = [];
  let start = -1;
  let opener = '';
  let closer = '';
  let depth = 0;
  let escaped = false;

  for (let i = 0; i < raw.length; i += 1) {
    const char = raw[i];
    if (start < 0) {
      if (char !== '@') continue;
      let cursor = i + 1;
      while (cursor < raw.length && /[A-Za-z]/.test(raw.charAt(cursor))) cursor += 1;
      while (cursor < raw.length && /\s/.test(raw.charAt(cursor))) cursor += 1;
      const opening = raw.charAt(cursor);
      if (opening !== '{' && opening !== '(') continue;
      start = i;
      opener = opening;
      closer = opener === '{' ? '}' : ')';
      depth = 1;
      i = cursor;
      continue;
    }
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === '\\') {
      escaped = true;
      continue;
    }
    if (char === opener) depth += 1;
    if (char === closer) depth -= 1;
    if (depth === 0) {
      entries.push(raw.slice(start, i + 1).trim());
      start = -1;
    }
  }

  if (start >= 0) entries.push(raw.slice(start).trim());
  if (entries.length === 0 && raw.trim()) entries.push(raw.trim());
  return entries;
}

export function AddPapersModal({
  pid = '',
  libraryId,
  open,
  onClose,
  onImported,
  initialMethod = 'arxiv',
}: {
  pid?: string;
  libraryId?: string;
  /** 打开时先选哪种方式（找论文页的「按编号 / BibTeX / Zotero」各自直达） */
  initialMethod?: ImportMethod;
  open: boolean;
  onClose: () => void;
  /** 添加成功 / 已存在时跳转选中该论文 */
  onImported: (paperId: string) => void;
}) {
  const queryClient = useQueryClient();
  const scopeId = libraryId ?? pid;
  const [method, setMethod] = useState<ImportMethod>(initialMethod);
  // 每次打开都从调用方指定的方式开始
  useEffect(() => {
    if (open) setMethod(initialMethod);
  }, [open, initialMethod]);
  const [arxivId, setArxivId] = useState('');
  const [doi, setDoi] = useState('');
  const [pmid, setPmid] = useState('');
  const [corpusId, setCorpusId] = useState('');
  const [bibtex, setBibtex] = useState('');
  const [zoteroBib, setZoteroBib] = useState<File | null>(null);
  const [zoteroZip, setZoteroZip] = useState<File | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ taskId: string; total: number } | null>(null);

  const invalidateLists = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['papers', scopeId] });
    void queryClient.invalidateQueries({ queryKey: ['ingest-state', scopeId] });
  }, [queryClient, scopeId]);

  const batchItems = useMemo<PaperBatchImportInput['items']>(() => {
    if (method === 'arxiv') return splitPaperInput(arxivId).map((value) => ({ arxiv_id: value }));
    if (method === 'doi') return splitPaperInput(doi).map((value) => ({ doi: value }));
    if (method === 'pmid') return splitPaperInput(pmid).map((value) => ({ pmid: value }));
    if (method === 'corpus') return splitPaperInput(corpusId).map((value) => ({ corpus_id: value }));
    return splitBibtexEntries(bibtex).map((value) => ({ bibtex: value }));
  }, [arxivId, bibtex, corpusId, doi, method, pmid]);
  const tooMany = batchItems.length > 50;

  const reset = () => {
    setArxivId('');
    setDoi('');
    setPmid('');
    setCorpusId('');
    setBibtex('');
    setZoteroBib(null);
    setZoteroZip(null);
    setParseError(null);
  };

  const importMutation = useMutation({
    mutationFn: (input: PaperBatchImportInput) => (
      libraryId ? api.importLibraryPapersBatch(libraryId, input) : api.importPapersBatch(pid, input)
    ),
    onSuccess: (task) => {
      reset();
      onClose();
      setProgress({ taskId: task.task_id, total: task.total });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        setParseError(
          tr('格式不正确，或超过 50 篇', 'The input isn’t valid, or has more than 50 papers.'),
        );
      } else if (e instanceof ApiError && e.status === 503) {
        setParseError(
          tr('本机引擎暂时无法处理，请稍后再试', 'The local engine is busy. Try again shortly.'),
        );
      } else {
        toast(`${tr('无法添加：', 'Couldn’t add: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  // Zotero 导入是文件上传（multipart），与逐条 JSON 的批量添加分开一条 mutation；
  // 结果复用同一个 paper-task 进度弹窗。
  const zoteroMutation = useMutation({
    mutationFn: ({ bib, zip }: { bib: File; zip: File | null }) =>
      api.importLibraryZotero(libraryId!, bib, zip),
    onSuccess: (task) => {
      reset();
      onClose();
      setProgress({ taskId: task.task_id, total: task.total });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        setParseError(
          tr('这个 .bib 文件里没有可识别的条目，或超过 500 条', 'No entries found in this .bib file, or it has more than 500.'),
        );
      } else if (e instanceof ApiError && e.status === 413) {
        setParseError(tr('文件太大，请在 Zotero 中分批导出', 'The file is too large. Export smaller batches from Zotero.'));
      } else if (e instanceof ApiError && e.status === 503) {
        setParseError(
          tr('本机引擎暂时无法处理，请稍后再试', 'The local engine is busy. Try again shortly.'),
        );
      } else {
        toast(`${tr('无法导入：', 'Couldn’t import: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  return (
    <>
    <Modal
      open={open}
      onClose={onClose}
      title={tr('添加论文', 'Add papers')}
      width={520}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={
              method === 'zotero'
                ? !zoteroBib || zoteroMutation.isPending
                : batchItems.length === 0 || tooMany || importMutation.isPending
            }
            onClick={() => {
              if (method === 'zotero') {
                if (libraryId && zoteroBib) zoteroMutation.mutate({ bib: zoteroBib, zip: zoteroZip });
              } else {
                importMutation.mutate({ items: batchItems });
              }
            }}
          >
            {(method === 'zotero' ? zoteroMutation.isPending : importMutation.isPending) ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {method === 'zotero' ? tr('导入中…', 'Importing…') : tr('添加中…', 'Adding…')}
              </>
            ) : method === 'zotero' ? (
              <>
                <Icon name="plus" size={13} />
                {tr('导入', 'Import')}
              </>
            ) : (
              <>
                <Icon name="plus" size={13} />
                {batchItems.length > 1
                  ? tr(`添加 ${batchItems.length} 篇`, `Add ${batchItems.length} papers`)
                  : tr('添加', 'Add')}
              </>
            )}
          </button>
        </>
      }
    >
      <Segmented<ImportMethod>
        options={[
          { v: 'arxiv', label: 'arXiv' },
          { v: 'doi', label: 'DOI' },
          { v: 'pmid', label: 'PMID' },
          { v: 'corpus', label: 'Corpus ID' },
          { v: 'bibtex', label: 'BibTeX' },
          ...(libraryId ? [{ v: 'zotero' as const, label: 'Zotero' }] : []),
        ]}
        value={method}
        onChange={(m) => {
          setMethod(m);
          setParseError(null);
        }}
      />
      <div style={{ marginTop: 14 }}>
        {method === 'arxiv' ? (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 112, resize: 'vertical', fontSize: 12 }}
              placeholder={tr(
                '例如 2405.01234, 2405.01235v2',
                'e.g. 2405.01234, 2405.01235v2',
              )}
              value={arxivId}
              onChange={(e) => {
                setArxivId(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr('可一次粘贴多个，最多 50 篇', 'Paste several at once, up to 50')}
            </div>
          </>
        ) : method === 'doi' ? (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 112, resize: 'vertical', fontSize: 12 }}
              placeholder={tr(
                '例如 10.1145/3567890.1234567',
                'e.g. 10.1145/3567890.1234567',
              )}
              value={doi}
              onChange={(e) => {
                setDoi(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr('可一次粘贴多个，最多 50 篇', 'Paste several at once, up to 50')}
            </div>
          </>
        ) : method === 'pmid' ? (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 112, resize: 'vertical', fontSize: 12 }}
              placeholder={tr(
                '例如 31452104, PMID:33301246',
                'e.g. 31452104, PMID:33301246',
              )}
              value={pmid}
              onChange={(e) => {
                setPmid(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr('可一次粘贴多个，最多 50 篇', 'Paste several at once, up to 50')}
            </div>
          </>
        ) : method === 'corpus' ? (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 112, resize: 'vertical', fontSize: 12 }}
              placeholder={tr(
                '例如 13756489, CorpusId:215416146',
                'e.g. 13756489, CorpusId:215416146',
              )}
              value={corpusId}
              onChange={(e) => {
                setCorpusId(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr('Semantic Scholar 编号，可一次粘贴多个，最多 50 篇', 'Semantic Scholar IDs. Paste several at once, up to 50.')}
            </div>
          </>
        ) : method === 'zotero' ? (
          <>
            <label className="col" style={{ gap: 6, fontSize: 12 }}>
              <span>{tr('BibTeX 文件（.bib）', 'BibTeX file (.bib)')}</span>
              <input
                type="file"
                accept=".bib,text/x-bibtex"
                onChange={(e) => {
                  setZoteroBib(e.target.files?.[0] ?? null);
                  setParseError(null);
                }}
              />
            </label>
            <label className="col" style={{ gap: 6, fontSize: 12, marginTop: 12 }}>
              <span>{tr('附件压缩包（可选，.zip）', 'Attachments (optional, .zip)')}</span>
              <input
                type="file"
                accept=".zip,application/zip"
                onChange={(e) => setZoteroZip(e.target.files?.[0] ?? null)}
              />
            </label>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr(
                '在 Zotero 中右键分类，选「导出分类」，格式选 BibTeX；勾选「导出文件」并把导出文件夹压缩成 zip 可带上 PDF。已有的论文会跳过。',
                'In Zotero, right-click a collection, choose Export Collection and pick BibTeX. To include PDFs, check “Export Files” and zip the exported folder. Papers already here are skipped.',
              )}
            </div>
          </>
        ) : (
          <>
            <textarea
              className="textarea mono"
              style={{ width: '100%', minHeight: 150, resize: 'vertical', fontSize: 12 }}
              placeholder={tr(
                '例如\n@inproceedings{smith2024example,\n  title = {...},\n  year = {2024},\n}',
                'e.g.\n@inproceedings{smith2024example,\n  title = {...},\n  year = {2024},\n}',
              )}
              value={bibtex}
              onChange={(e) => {
                setBibtex(e.target.value);
                setParseError(null);
              }}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
              {tr(
                '可一次粘贴多条，最多 50 条，每条需有 title',
                'Paste several entries, up to 50. Each needs a title.',
              )}
            </div>
          </>
        )}
        {tooMany && (
          <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--danger-tx)' }}>
            {tr(`识别到 ${batchItems.length} 篇，一次最多 50 篇`, `Found ${batchItems.length}. The limit is 50 at a time.`)}
          </div>
        )}
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
            {parseError}
          </div>
        )}
      </div>
    </Modal>
    {progress && (
      <PaperBatchProgressModal
        taskId={progress.taskId}
        total={progress.total}
        onClose={() => setProgress(null)}
        onDone={invalidateLists}
        onOpenPaper={(paperId) => {
          onImported(paperId);
          setProgress(null);
        }}
      />
    )}
    </>
  );
}
