import { useState, type ReactNode } from 'react';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { SearchInput } from '../wiki/shared';
import { tr } from '../../lib/i18n';

/* ============================================================
   回收站弹窗（共享外壳）：桶内客户端搜索 + 召回 / 彻底删除 /
   清空（带二次确认）+ 空态 + 条数截断提示。
   列表数据和四个动作全部由调用方注入，组件本身不碰任何端点，
   实验室文献库（PapersTab）与课题相关研究（ResearchPage）共用。
   ============================================================ */

/** 回收站里一行的展示模型：调用方把自己的数据结构映射成这个。 */
export interface TrashItemView {
  id: string;
  /** 顶行 mono 编号（arXiv 编号 / 期刊会议，都没有就给 '—'） */
  code: string;
  year: number | null;
  title: string;
  /** 编号 / 年份之后的小图标位（如「有解读」星标） */
  leading?: ReactNode;
  /** 顶行最右侧（如相关度条、移入回收站的时间） */
  aside?: ReactNode;
  /** 标题下方的标签行（删除原因等） */
  tags?: ReactNode;
  /** 标签行右侧的一句话摘要（超出省略） */
  desc?: string | null;
  /** 桶内客户端搜索的匹配文本（标题 + 作者，调用方拼好） */
  searchText: string;
}

export interface TrashModalProps {
  open: boolean;
  onClose: () => void;
  /** 弹窗副标题：说明这个回收站装的是什么 */
  sub: ReactNode;
  items: TrashItemView[];
  /** 后端总条数：大于已加载条数时提示「仅显示最近 N 篇」；不分页可省略 */
  total?: number;
  loading: boolean;
  /** 任一动作进行中：禁用全部按钮 */
  busy: boolean;
  emptying: boolean;
  onRestore: (id: string) => void;
  onPurge: (id: string) => void;
  onEmpty: () => void;
  /** 清空确认里的危险说明（各处删掉的东西不一样，文案由调用方给） */
  emptyWarning: (count: number) => ReactNode;
  /** 行内两个按钮的 hover 说明（同上，各处含义略有差别） */
  restoreHint?: string;
  purgeHint?: string;
  width?: number;
}

export function TrashModal({
  open,
  onClose,
  sub,
  items: allItems,
  total,
  loading,
  busy,
  emptying,
  onRestore,
  onPurge,
  onEmpty,
  emptyWarning,
  restoreHint,
  purgeHint,
  width = 680,
}: TrashModalProps) {
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const [trashQ, setTrashQ] = useState('');

  // 桶内搜索：标题 / 作者（客户端过滤）
  const kw = trashQ.trim().toLowerCase();
  const items = kw ? allItems.filter((it) => it.searchText.toLowerCase().includes(kw)) : allItems;
  // 清空成功后桶空了 → 自动退出确认态，footer 不会停在「确认清空 0 篇」
  const confirming = confirmEmpty && allItems.length > 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('回收站', 'Trash')}
      sub={sub}
      width={width}
      footer={
        <>
          {confirming ? (
            <>
              <span style={{ fontSize: 12, color: 'var(--danger-tx)', marginRight: 'auto' }}>
                {emptyWarning(allItems.length)}
              </span>
              <button className="btn btn-ghost sm" onClick={() => setConfirmEmpty(false)}>
                {tr('取消', 'Cancel')}
              </button>
              <button
                className="btn btn-primary sm"
                style={{ background: 'var(--danger-tx)' }}
                disabled={busy}
                onClick={onEmpty}
              >
                {emptying ? tr('清空中…', 'Emptying…') : tr('确认清空', 'Confirm empty')}
              </button>
            </>
          ) : (
            <>
              <button
                className="btn btn-ghost sm"
                style={{ color: 'var(--danger-tx)', marginRight: 'auto' }}
                disabled={allItems.length === 0 || busy}
                onClick={() => setConfirmEmpty(true)}
              >
                <Icon name="x" size={12} />
                {tr('清空回收站', 'Empty trash')}
              </button>
              <button className="btn btn-soft sm" onClick={onClose}>
                {tr('关闭', 'Close')}
              </button>
            </>
          )}
        </>
      }
    >
      {/* 搜索区固定不随列表滚动：列表自带滚动容器，整体高度不超出 Modal 内容区 */}
      <div className="row gap10" style={{ marginBottom: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <SearchInput value={trashQ} onChange={setTrashQ} placeholder={tr('搜索标题 / 作者…', 'Search title / author…')} />
        </div>
        <span className="mono muted" style={{ fontSize: 11, flexShrink: 0 }}>
          {kw
            ? tr(`${items.length} / ${allItems.length} 篇`, `${items.length} / ${allItems.length}`)
            : tr(`${allItems.length} 篇`, `${allItems.length} papers`)}
        </span>
      </div>
      {loading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : allItems.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr('回收站是空的', 'Trash is empty')}</div>
      ) : items.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr('没有匹配的文献', 'No matching papers')}</div>
      ) : (
        <div
          className="scroll"
          style={{
            maxHeight: '46vh',
            overflowY: 'auto',
            border: '0.5px solid var(--border)',
            borderRadius: 10,
          }}
        >
          {items.map((it, i) => (
            <TrashRow
              key={it.id}
              item={it}
              last={i === items.length - 1}
              busy={busy}
              restoreHint={restoreHint}
              purgeHint={purgeHint}
              onRestore={() => onRestore(it.id)}
              onPurge={() => onPurge(it.id)}
            />
          ))}
          {(total ?? 0) > allItems.length && (
            <div className="muted" style={{ fontSize: 11, textAlign: 'center', padding: 8 }}>
              {tr(
                `仅显示最近 ${allItems.length} 篇（共 ${total} 篇）`,
                `Showing the latest ${allItems.length} (of ${total})`,
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/** 回收站列表行：与论文库 PaperRow 同款版式，右侧召回 / 彻底删除。 */
function TrashRow({
  item,
  last,
  busy,
  restoreHint,
  purgeHint,
  onRestore,
  onPurge,
}: {
  item: TrashItemView;
  last: boolean;
  busy: boolean;
  restoreHint?: string;
  purgeHint?: string;
  onRestore: () => void;
  onPurge: () => void;
}) {
  return (
    <div
      className="row gap10"
      style={{
        padding: '12px 16px',
        borderBottom: last ? 'none' : '0.5px solid var(--border)',
        alignItems: 'flex-start',
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap8" style={{ marginBottom: 5 }}>
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
            {item.code}
          </span>
          {item.year !== null && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {item.year}
            </span>
          )}
          {item.leading}
          <span style={{ marginLeft: 'auto' }}>{item.aside}</span>
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>{item.title}</div>
        {(item.tags || item.desc) && (
          <div className="row gap8" style={{ marginTop: 6 }}>
            {item.tags}
            {item.desc && (
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 11.5,
                  color: 'var(--text-3)',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {item.desc}
              </span>
            )}
          </div>
        )}
      </div>
      <div className="col" style={{ gap: 6, flexShrink: 0 }}>
        <button
          className="btn btn-soft sm"
          style={{ height: 26 }}
          disabled={busy}
          title={restoreHint ?? tr('召回到论文库', 'Restore to the library')}
          onClick={onRestore}
        >
          <Icon name="refresh" size={12} />
          {tr('召回', 'Restore')}
        </button>
        <button
          className="btn btn-ghost sm"
          style={{ height: 26, color: 'var(--danger-tx)' }}
          disabled={busy}
          title={purgeHint ?? tr('彻底删除（连同文件，无法恢复）', 'Delete permanently (files included, no undo)')}
          onClick={onPurge}
        >
          <Icon name="x" size={12} />
          {tr('彻底删除', 'Delete forever')}
        </button>
      </div>
    </div>
  );
}
