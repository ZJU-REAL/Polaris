import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   会话列表：停靠栏里的常驻一栏。

   原来历史藏在头部一个时钟按钮后面，点开是个浮层——找上次那场对话得先想起来"它在
   时钟里"。列表常驻之后，切换会话和新开一场都只要一次点击。

   按最近活跃分组（今天 / 昨天 / 7 天内 / 更早）：几十场对话平铺成一列时，"上周那次"
   根本找不到，而时间恰好是人回忆对话时用的线索。
   ============================================================ */

export interface ConversationRow {
  id: string;
  title: string;
  last_message_at?: string | null;
}

/** 分组：只按天算，不显示具体时刻——对话列表要的是"哪一段时间"，不是精确到分。 */
export function groupByRecency(
  rows: ConversationRow[],
  now: Date,
): { label: string; rows: ConversationRow[] }[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  const today: ConversationRow[] = [];
  const yesterday: ConversationRow[] = [];
  const week: ConversationRow[] = [];
  const older: ConversationRow[] = [];
  for (const row of rows) {
    const at = row.last_message_at ? new Date(row.last_message_at).getTime() : 0;
    // 没有时间戳的（刚建、还没说过话）归到今天：它就在眼前，扔进「更早」很怪
    if (!at || at >= startOfToday) today.push(row);
    else if (at >= startOfToday - day) yesterday.push(row);
    else if (at >= startOfToday - 7 * day) week.push(row);
    else older.push(row);
  }
  return [
    { label: tr('今天', 'Today'), rows: today },
    { label: tr('昨天', 'Yesterday'), rows: yesterday },
    { label: tr('7 天内', 'Previous 7 days'), rows: week },
    { label: tr('更早', 'Older'), rows: older },
  ].filter((b) => b.rows.length > 0);
}

export function ConversationRail({
  activeId,
  onPick,
  onNew,
  refreshKey,
  runningId,
}: {
  activeId: string | null;
  onPick: (id: string) => void;
  onNew: () => void;
  /** 变一次就重拉一次列表（发完一轮之后标题才有） */
  refreshKey: number;
  /** 正在跑的那场：列表里给它一个呼吸灯 */
  runningId?: string | null;
}) {
  const [rows, setRows] = useState<ConversationRow[]>([]);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  const reload = useCallback(async () => {
    try {
      setRows((await api.listAssistantConversations()) as ConversationRow[]);
    } catch {
      setRows([]); // 拉不到就空着，不摆一个假列表
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const remove = useCallback(
    async (id: string) => {
      try {
        await api.deleteAssistantConversation(id);
      } catch {
        return; // 删不掉就别把它从界面上抹掉，那是在骗人
      }
      await reload();
      if (id === activeId) onNew();
    },
    [activeId, onNew, reload],
  );

  const commitRename = useCallback(
    async (id: string) => {
      const title = draft.trim();
      setRenaming(null);
      if (!title) return;
      try {
        await api.renameAssistantConversation(id, title);
      } catch {
        return;
      }
      await reload();
    },
    [draft, reload],
  );

  return (
    <div
      style={{
        width: 176,
        flexShrink: 0,
        borderRight: '0.5px solid var(--border-2)',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--surface-2)',
      }}
    >
      <button
        className="btn btn-soft sm"
        onClick={onNew}
        style={{ margin: 8, height: 28, fontSize: 12, justifyContent: 'center' }}
      >
        <Icon name="plus" size={12} /> {tr('新对话', 'New chat')}
      </button>
      <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '0 6px 8px' }}>
        {rows.length === 0 && (
          <div style={{ fontSize: 11, color: 'var(--text-4)', padding: '6px 6px' }}>
            {tr('还没有对话', 'No conversations yet')}
          </div>
        )}
        {groupByRecency(rows, new Date()).map((group) => (
          <div key={group.label} style={{ marginBottom: 6 }}>
            <div
              style={{
                fontSize: 10,
                color: 'var(--text-4)',
                padding: '6px 6px 3px',
                letterSpacing: '0.04em',
              }}
            >
              {group.label}
            </div>
            {group.rows.map((row) => (
              <div
                key={row.id}
                className="row gap4 hoverable"
                onClick={() => renaming !== row.id && onPick(row.id)}
                style={{
                  alignItems: 'center',
                  padding: '5px 6px',
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: row.id === activeId ? 'var(--accent-soft)' : undefined,
                }}
              >
                {row.id === runningId && <span className="buddy-live-dot" />}
                {renaming === row.id ? (
                  <input
                    className="input"
                    autoFocus
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onBlur={() => void commitRename(row.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void commitRename(row.id);
                      if (e.key === 'Escape') setRenaming(null);
                    }}
                    style={{ height: 22, fontSize: 11.5, flex: 1, minWidth: 0 }}
                  />
                ) : (
                  <>
                    <span
                      style={{
                        flex: 1,
                        minWidth: 0,
                        fontSize: 11.5,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        color: row.id === activeId ? 'var(--accent-text)' : 'var(--text-2)',
                      }}
                      title={row.title || tr('（未命名）', '(untitled)')}
                    >
                      {row.title || tr('（未命名）', '(untitled)')}
                    </span>
                    <span
                      className="icon-btn"
                      title={tr('重命名', 'Rename')}
                      onClick={(e) => {
                        e.stopPropagation();
                        setDraft(row.title);
                        setRenaming(row.id);
                      }}
                      style={{ width: 18, height: 18, flexShrink: 0 }}
                    >
                      <Icon name="pen" size={10} />
                    </span>
                    <span
                      className="icon-btn"
                      title={tr('删除', 'Delete')}
                      onClick={(e) => {
                        e.stopPropagation();
                        void remove(row.id);
                      }}
                      style={{ width: 18, height: 18, flexShrink: 0 }}
                    >
                      <Icon name="trash" size={10} />
                    </span>
                  </>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
