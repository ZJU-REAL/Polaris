import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type PaperIndexStatus, type VectorStatus } from '../../lib/api';
import { fmtFullTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { errorText } from '../../lib/errors';
import { toast } from './Toast';
import { Icon } from './Icon';

/**
 * 论文检索索引状态：两个红绿点（论文级向量 / 全文分块向量）+ 手动构建按钮。
 *
 * 红绿点鼠标悬浮显示构建时间与所用模型（存量数据没记过这两项，就只说建过了）。
 * 「构建 / 重新构建」按钮的文案随有没有索引切换——已有就是重建（覆盖旧向量）。
 *
 * 「待重建」（黄点）是换过向量模型之后的状态：向量还在，但出自旧模型，检索用不上
 * 它了。与「从没建过」分开显示，否则换完模型满屏红点，看着像数据丢了。
 */

function dotTitle(label: string, s: VectorStatus | undefined, note?: string): string {
  const head = (() => {
    const when = s?.built_at ? fmtFullTime(s.built_at) : null;
    if (s?.stale) {
      return tr(`${label}：需要重建（嵌入模型已更换）`, `${label}: needs rebuild (embedding model changed)`);
    }
    if (!s?.built) return tr(`${label}：未建立`, `${label}: not built`);
    if (s.model && when) {
      return tr(`${label}：${when} 建立，模型 ${s.model}`, `${label}: built ${when} with ${s.model}`);
    }
    if (s.model) return tr(`${label}：已建立，模型 ${s.model}`, `${label}: built with ${s.model}`);
    if (when) return tr(`${label}：${when} 建立`, `${label}: built ${when}`);
    // 存量数据：向量在，但没记过时间与模型名
    return tr(`${label}：已建立`, `${label}: built`);
  })();
  return note ? `${head}\n${note}` : head;
}

function Dot({
  label,
  status,
  note,
  /** 部分可用（只有摘要级索引）→ 黄点，别让人以为已经有全文索引了 */
  partial,
}: {
  label: string;
  status: VectorStatus | undefined;
  note?: string;
  partial?: boolean;
}) {
  const built = !!status?.built;
  // 待重建也走黄点：有东西但当前用不上，和「从没建过」的红点区别开
  const color = status?.stale
    ? 'var(--warn)'
    : !built
      ? 'var(--danger)'
      : partial
        ? 'var(--warn)'
        : 'var(--ok)';
  return (
    <span
      className="row gap6"
      title={dotTitle(label, status, note)}
      style={{ alignItems: 'center', cursor: 'help' }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: color,
          flexShrink: 0,
          display: 'inline-block',
        }}
      />
      <span style={{ color: 'var(--text-3)', fontSize: 12 }}>{label}</span>
    </span>
  );
}

export function PaperIndexStatusRow({
  paperId,
  showRebuild = true,
}: {
  paperId: string;
  /** 放进顶部徽章行时关掉按钮：那一行是「这篇论文是什么」的速览，不是操作区。 */
  showRebuild?: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = ['paper-index-status', paperId];

  const { data, isLoading, isError } = useQuery({
    queryKey,
    queryFn: () => api.getPaperIndexStatus(paperId),
    retry: false,
  });

  const rebuild = useMutation({
    mutationFn: () => api.rebuildPaperIndex(paperId),
    onSuccess: (status) => {
      queryClient.setQueryData<PaperIndexStatus>(queryKey, status);
      toast(tr('索引已建立', 'Index built'), 'ok');
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError && e.status === 403
          ? tr('没有可用的嵌入模型，请在设置中配置', 'No embedding model available. Set one up in Settings.')
          : errorText(e);
      toast(`${tr('无法建立索引：', 'Couldn’t build the index: ')}${msg}`, 'error');
    },
  });

  // 状态查询失败（例如后端还没上这两个端点）就整块不渲染，不打扰阅读
  if (isError) return null;

  const built = !!data && (data.paper_vector.built || data.chunk_vector.built);
  // 只有摘要兜底块 = 这篇没有全文索引，只是「能搜到」而已，别让绿点造成误会
  const abstractOnly = data?.chunk_source === 'abstract';
  const chunkLabel = abstractOnly
    ? tr('摘要检索', 'Abstract search')
    : tr('全文检索', 'Full-text search');
  const chunkNote = abstractOnly
    ? tr(
        '还没有全文，只索引了标题和摘要。获取 PDF 后会改为全文索引。',
        'No full text yet, so only the title and abstract are indexed. Fetching the PDF switches it to full text.',
      )
    : undefined;

  return (
    <div className="row gap12 wrap" style={{ alignItems: 'center' }}>
      <Dot label={tr('整篇检索', 'Paper search')} status={data?.paper_vector} />
      <Dot
        label={chunkLabel}
        status={data?.chunk_vector}
        note={chunkNote}
        partial={abstractOnly}
      />
      {data && data.chunk_count > 0 && !abstractOnly && (
        <span
          className="mono"
          style={{ color: 'var(--text-3)', fontSize: 11 }}
          title={tr('已索引段落 / 段落总数', 'Indexed passages / total passages')}
        >
          {data.embedded_chunk_count}/{data.chunk_count}
        </span>
      )}
      {showRebuild && (
      <button
        type="button"
        className="btn btn-ghost sm"
        disabled={isLoading || rebuild.isPending}
        onClick={() => rebuild.mutate()}
        title={tr('重新建立这篇论文的检索索引', 'Rebuild this paper’s search index')}
      >
        <Icon
          name="refresh"
          size={11}
          style={rebuild.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
        />
        {rebuild.isPending
          ? tr('建立中…', 'Building…')
          : built
            ? tr('重建索引', 'Rebuild index')
            : tr('建立索引', 'Build index')}
      </button>
      )}
    </div>
  );
}
