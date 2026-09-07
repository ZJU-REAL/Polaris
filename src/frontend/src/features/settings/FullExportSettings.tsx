/* 一键全量导出（#690，「随时可走」做进界面）：
   点按钮 → 入队后台任务 → SSE 收各面进度 → 完成后就地下载 zip。
   进度复用 paper-task 事件通道（与 Zotero 导入同口径），不新起轮询。 */
import { useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { subscribeSse } from '../../lib/sse';
import { saveBlob } from '../wiki/shared';

type Phase = 'idle' | 'running' | 'done' | 'error';

interface ExportCounts {
  [key: string]: number;
}

/* 面/计数的展示名放函数里取——模块级常量不能顶层调 tr（语言切换后会失效） */
function facetLabel(facet: string): string {
  switch (facet) {
    case 'libraries':
      return tr('文献库', 'Libraries');
    case 'notes':
      return tr('笔记与划线', 'Notes & highlights');
    case 'wiki':
      return tr('论文解读', 'Paper interpretations');
    case 'discovery':
      return tr('假设探索', 'Hypothesis discovery');
    case 'experiments':
      return tr('实验记录', 'Experiments');
    case 'manuscripts':
      return tr('论文稿件', 'Manuscripts');
    default:
      return facet;
  }
}

function countSummary(counts: ExportCounts): string {
  const parts: string[] = [];
  const push = (n: number | undefined, zh: string, en: string) => {
    if (n) parts.push(tr(`${n} ${zh}`, `${n} ${en}`));
  };
  push(counts.libraries, '个文献库', 'libraries');
  push(counts.papers, '篇论文', 'papers');
  push(counts.pdfs, '份 PDF', 'PDFs');
  push(counts.notes, '条笔记', 'notes');
  push(counts.highlights, '条划线', 'highlights');
  push(counts.wiki_pages, '页解读', 'wiki pages');
  push(counts.discovery_runs, '个探索任务', 'discovery runs');
  push(counts.experiments, '个实验', 'experiments');
  push(counts.manuscripts, '篇稿件', 'manuscripts');
  return parts.length > 0 ? parts.join(tr('、', ', ')) : tr('还没有数据，导出的是空骨架', 'No data yet — the archive is an empty skeleton');
}

export function FullExportSettings() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [doneFacets, setDoneFacets] = useState<string[]>([]);
  const [counts, setCounts] = useState<ExportCounts>({});
  const [warnings, setWarnings] = useState<number>(0);
  const [errorMsg, setErrorMsg] = useState<string>('');
  const [starting, setStarting] = useState(false);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    if (phase !== 'running' || !taskId) return;
    const cancel = subscribeSse(`/paper-tasks/${taskId}/events`, {
      onEvent(event, data) {
        let payload: any = {};
        try {
          payload = JSON.parse(data);
        } catch {
          return;
        }
        if (event === 'export_progress') {
          setDoneFacets((prev) => (prev.includes(payload.facet) ? prev : [...prev, payload.facet]));
          setCounts(payload.counts ?? {});
        } else if (event === 'done') {
          setCounts(payload.counts ?? {});
          setWarnings((payload.warnings ?? []).length);
          setPhase('done');
        } else if (event === 'error') {
          setErrorMsg(String(payload.message ?? ''));
          setPhase('error');
        }
      },
    });
    return cancel;
  }, [phase, taskId]);

  async function start() {
    setStarting(true);
    try {
      const { task_id } = await api.startFullExport();
      setTaskId(task_id);
      setDoneFacets([]);
      setCounts({});
      setWarnings(0);
      setErrorMsg('');
      setPhase('running');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        toast(tr('已经有一个导出在进行中，请稍候', 'An export is already running. Please wait.'), 'error');
      } else {
        const message = error instanceof Error ? error.message : String(error);
        toast(`${tr('导出启动失败', 'Could not start export')}: ${message}`, 'error');
      }
    } finally {
      setStarting(false);
    }
  }

  async function download() {
    if (!taskId) return;
    setDownloading(true);
    try {
      const blob = await api.downloadFullExport(taskId);
      saveBlob(blob, 'polaris-full-export.zip');
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('下载失败', 'Download failed')}: ${message}`, 'error');
    } finally {
      setDownloading(false);
    }
  }

  return (
    <section className="card card-pad" style={{ maxWidth: 880 }}>
      <div
        className="row"
        style={{ justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}
      >
        <div style={{ flex: '1 1 420px', minWidth: 0 }}>
          <div className="section-h">
            <Icon name="download" size={15} style={{ color: 'var(--accent)' }} />
            {tr('全量导出', 'Full export')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 5, lineHeight: 1.65 }}>
            {tr(
              '把你在 Polaris 里的全部数据打包带走：文献库和 PDF、笔记划线、论文解读、假设探索、实验记录、论文稿件，全部装进一个 zip。里面都是普通的 Markdown、BibTeX 和 JSON 文件，离开平台也能直接用。',
              'Take all your Polaris data with you: libraries and PDFs, notes and highlights, paper interpretations, hypothesis discovery runs, experiment records and manuscripts, packed into one zip of plain Markdown, BibTeX and JSON files that work anywhere.',
            )}
          </div>
        </div>
        <button
          className="btn btn-primary"
          disabled={starting || phase === 'running'}
          onClick={() => void start()}
        >
          <Icon name={phase === 'running' ? 'refresh' : 'download'} size={14} />
          {phase === 'running'
            ? tr('打包中...', 'Packing...')
            : starting
              ? tr('启动中...', 'Starting...')
              : tr('打包我的全部数据', 'Pack all my data')}
        </button>
      </div>

      {phase !== 'idle' && (
        <div
          style={{
            marginTop: 18,
            padding: 14,
            background: 'var(--surface-2)',
            border: '1px solid var(--border)',
            borderRadius: 6,
            fontSize: 12.5,
            lineHeight: 1.8,
          }}
        >
          {doneFacets.map((facet) => (
            <div key={facet} className="row" style={{ gap: 8, alignItems: 'center' }}>
              <Icon name="check" size={13} style={{ color: 'var(--ok-tx, var(--accent))' }} />
              <span>{facetLabel(facet)}</span>
            </div>
          ))}
          {phase === 'running' && (
            <div style={{ color: 'var(--text-3)', marginTop: doneFacets.length > 0 ? 6 : 0 }}>
              {tr('正在收拾你的数据，请稍等……', 'Gathering your data, one moment...')}
            </div>
          )}
          {phase === 'done' && (
            <div style={{ marginTop: 8 }}>
              <div style={{ color: 'var(--text-2)' }}>
                {tr('打包完成：', 'All packed: ')}
                {countSummary(counts)}
                {warnings > 0 &&
                  tr(
                    `（有 ${warnings} 项没能装进去，详见包内 manifest.json）`,
                    ` (${warnings} item(s) could not be included — see manifest.json inside the archive)`,
                  )}
              </div>
              <button
                className="btn btn-soft sm"
                disabled={downloading}
                onClick={() => void download()}
                style={{ marginTop: 10 }}
              >
                <Icon name="download" size={13} />
                {downloading ? tr('下载中...', 'Downloading...') : tr('下载 zip', 'Download zip')}
              </button>
            </div>
          )}
          {phase === 'error' && (
            <div style={{ color: 'var(--danger-tx)', marginTop: 6 }}>
              {tr('导出没成功', 'Export failed')}
              {errorMsg ? `: ${errorMsg}` : ''}
              {tr('。可以稍后再试一次。', ' You can try again later.')}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
