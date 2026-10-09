/* 一键全量导出（#690，「随时可走」做进界面）：
   点按钮 → 入队后台任务 → SSE 收各面进度 → 完成后就地下载 zip。
   进度复用 paper-task 事件通道（与 Zotero 导入同口径），不新起轮询。 */
import { useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack } from './settingsUi';
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
      return tr('解读', 'Summaries');
    case 'discovery':
      return tr('假设探索', 'Hypothesis exploration');
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
  push(counts.wiki_pages, '篇解读', 'summaries');
  push(counts.discovery_runs, '次假设探索', 'explorations');
  push(counts.experiments, '个实验', 'experiments');
  push(counts.manuscripts, '篇稿件', 'manuscripts');
  return parts.length > 0 ? parts.join(tr('、', ', ')) : tr('还没有数据，导出的文件是空的', 'No data yet, so the archive is empty');
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
        toast(tr('已有一个导出正在进行，请等它完成', 'An export is already running. Wait for it to finish.'), 'error');
      } else {
        const message = error instanceof Error ? error.message : String(error);
        toast(`${tr('无法开始导出', 'Couldn’t start the export')}：${message}`, 'error');
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
      toast(`${tr('下载失败', 'Download failed')}：${message}`, 'error');
    } finally {
      setDownloading(false);
    }
  }

  return (
    <SettingsStack>
    <SettingsSection title={tr('导出', 'Export')}>
      <SettingsGroup>
      <SettingsRow
        label={tr('导出全部数据', 'Export all data')}
        hint={tr('文献库、PDF、笔记、解读、实验和稿件，打包为一个 zip', 'Libraries, PDFs, notes, summaries, experiments and manuscripts in one zip')}
      >
        <button
          className={phase === 'done' ? 'btn btn-ghost sm' : 'btn btn-primary sm'}
          disabled={starting || phase === 'running'}
          onClick={() => void start()}
        >
          {phase === 'running'
            ? tr('正在打包…', 'Packing…')
            : starting
              ? tr('正在开始…', 'Starting…')
              : tr('开始导出', 'Start export')}
        </button>
      </SettingsRow>

      {phase !== 'idle' && (
        <div className="st-row st-row-stack" style={{ fontSize: 13, lineHeight: 1.8, gap: 0 }}>
          {doneFacets.map((facet) => (
            <div key={facet} className="row" style={{ gap: 8, alignItems: 'center' }}>
              <Icon name="check" size={13} style={{ color: 'var(--ok-tx, var(--accent))' }} />
              <span>{facetLabel(facet)}</span>
            </div>
          ))}
          {phase === 'running' && (
            <div style={{ color: 'var(--text-3)', marginTop: doneFacets.length > 0 ? 6 : 0 }}>
              {tr('正在打包…', 'Packing…')}
            </div>
          )}
          {phase === 'done' && (
            <div style={{ marginTop: 8 }}>
              <div style={{ color: 'var(--text-2)' }}>
                {tr('已打包：', 'Packed: ')}
                {countSummary(counts)}
                {warnings > 0 &&
                  tr(
                    `（${warnings} 项未能导出，详见压缩包内的 manifest.json）`,
                    warnings === 1
                      ? ' (1 item couldn’t be exported. See manifest.json in the zip.)'
                      : ` (${warnings} items couldn’t be exported. See manifest.json in the zip.)`,
                  )}
              </div>
              <button
                className="btn btn-primary sm"
                disabled={downloading}
                onClick={() => void download()}
                style={{ marginTop: 10 }}
              >
                <Icon name="download" size={13} />
                {downloading ? tr('正在下载…', 'Downloading…') : tr('下载 zip', 'Download zip')}
              </button>
            </div>
          )}
          {phase === 'error' && (
            <div style={{ color: 'var(--danger-tx)', marginTop: 6 }}>
              {tr('导出失败，请重试', 'Export failed. Try again.')}
              {errorMsg && <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{errorMsg}</div>}
            </div>
          )}
        </div>
      )}
      </SettingsGroup>
    </SettingsSection>
    </SettingsStack>
  );
}
