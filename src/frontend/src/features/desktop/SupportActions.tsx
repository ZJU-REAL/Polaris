import { useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { copyText } from '../../lib/clipboard';
import { hostDiagnostics, openLogsFolder } from '../../lib/host';
import { tr } from '../../lib/i18n';

export type SupportNote = 'copied' | 'copy-failed' | 'open-failed' | null;

/**
 * 复制诊断信息：取 → 写剪贴板 → 说结果。取不到（老外壳没有这个方法）或写不进
 * 剪贴板都如实说「没复制成」，不留一个点了没反应的按钮。
 */
export async function runCopyDiagnostics(
  fetchDiagnostics: () => Promise<{ text: string } | null>,
  write: (text: string) => Promise<boolean>,
): Promise<SupportNote> {
  try {
    const diagnostics = await fetchDiagnostics();
    if (!diagnostics?.text) return 'copy-failed';
    return (await write(diagnostics.text)) ? 'copied' : 'copy-failed';
  } catch {
    return 'copy-failed';
  }
}

/** 一行反馈文案；模块顶层不能调 tr，所以是函数。 */
export function supportNoteText(note: SupportNote): string {
  switch (note) {
    case 'copied':
      return tr('已复制', 'Copied');
    case 'copy-failed':
      return tr('无法复制诊断信息', 'Couldn’t copy diagnostics');
    case 'open-failed':
      return tr('无法打开日志文件夹', 'Couldn’t open the logs folder');
    default:
      return '';
  }
}

/**
 * 「打开日志文件夹」「复制诊断信息」两个按钮。设置 → 关于、安装失败页、
 * 引擎不可用页用的是同一对：用户在哪儿遇到问题，都能拿到同一份东西发给我们。
 */
export function SupportActions({
  size = 'sm',
  initialNote = null,
}: {
  size?: 'sm' | 'md';
  /** 仅测试用：直接画出某条反馈 */
  initialNote?: SupportNote;
}) {
  const [note, setNote] = useState<SupportNote>(initialNote);
  const [copying, setCopying] = useState(false);
  const cls = size === 'sm' ? 'btn btn-ghost sm' : 'btn btn-ghost';

  const open = async () => {
    setNote((await openLogsFolder()) ? null : 'open-failed');
  };
  const copy = async () => {
    setCopying(true);
    try {
      setNote(await runCopyDiagnostics(hostDiagnostics, copyText));
    } finally {
      setCopying(false);
    }
  };

  return (
    <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
      <button type="button" className={cls} onClick={() => void open()}>
        <Icon name="file" size={13} />
        {tr('打开日志文件夹', 'Open logs folder')}
      </button>
      <button type="button" className={cls} disabled={copying} onClick={() => void copy()}>
        {tr('复制诊断信息', 'Copy diagnostics')}
      </button>
      {note && (
        <span
          role="status"
          style={{ fontSize: 12, color: note === 'copied' ? 'var(--ok-tx)' : 'var(--warn-tx)' }}
        >
          {supportNoteText(note)}
        </span>
      )}
    </div>
  );
}
