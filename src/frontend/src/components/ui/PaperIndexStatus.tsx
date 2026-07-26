import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type PaperIndexStatus, type VectorStatus } from '../../lib/api';
import { fmtFullTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { toast } from './Toast';
import { Icon } from './Icon';

/**
 * 论文检索索引状态：两个红绿点（论文级向量 / 全文分块向量）+ 手动构建按钮。
 *
 * 红绿点鼠标悬浮显示构建时间与所用模型（存量数据没记过这两项，就只说建过了）。
 * 「构建 / 重新构建」按钮的文案随有没有索引切换——已有就是重建（覆盖旧向量）。
 */

function dotTitle(label: string, s: VectorStatus | undefined, note?: string): string {
  const head = (() => {
    if (!s?.built) return tr(`${label}：未构建`, `${label}: not built`);
    const when = s.built_at ? fmtFullTime(s.built_at) : null;
    if (s.model && when) {
      return tr(`${label}：${when} 由 ${s.model} 构建`, `${label}: built ${when} by ${s.model}`);
    }
    if (s.model) return tr(`${label}：由 ${s.model} 构建`, `${label}: built by ${s.model}`);
    if (when) return tr(`${label}：${when} 构建`, `${label}: built ${when}`);
    // 存量数据：向量在，但没记过时间与模型名
    return tr(`${label}：已构建（时间与模型未记录）`, `${label}: built (time and model not recorded)`);
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
  const color = !built ? 'var(--danger)' : partial ? 'var(--warn)' : 'var(--ok)';
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
      <span style={{ color: 'var(--text-3)', fontSize: 11.5 }}>{label}</span>
    </span>
  );
}

export function PaperIndexStatusRow({ paperId }: { paperId: string }) {
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
      toast(tr('索引已构建', 'Index built'), 'ok');
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError && e.status === 403
          ? tr('没有大模型使用权限，无法构建索引', 'No LLM access — cannot build the index')
          : e instanceof Error
            ? e.message
            : String(e);
      toast(`${tr('构建失败：', 'Build failed: ')}${msg}`, 'error');
    },
  });

  // 状态查询失败（例如后端还没上这两个端点）就整块不渲染，不打扰阅读
  if (isError) return null;

  const built = !!data && (data.paper_vector.built || data.chunk_vector.built);
  // 只有摘要兜底块 = 这篇没有全文索引，只是「能搜到」而已，别让绿点造成误会
  const abstractOnly = data?.chunk_source === 'abstract';
  const chunkLabel = abstractOnly
    ? tr('摘要级索引', 'Abstract-level index')
    : tr('全文分块向量', 'Chunk vectors');
  const chunkNote = abstractOnly
    ? tr(
        '这篇没有全文，只用标题 + 摘要建了一个分段（向量与论文级向量是同一份）。取到 PDF 后会换成全文分块。',
        'No full text for this paper — only one segment from title and abstract (sharing the paper vector). It is replaced by full-text chunks once a PDF is fetched.',
      )
    : undefined;

  return (
    <div className="row gap12 wrap" style={{ alignItems: 'center' }}>
      <Dot label={tr('论文级向量', 'Paper vector')} status={data?.paper_vector} />
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
          title={tr('已建向量的分段 / 分段总数', 'Segments with vectors / total segments')}
        >
          {data.embedded_chunk_count}/{data.chunk_count}
        </span>
      )}
      <button
        type="button"
        className="btn btn-ghost sm"
        disabled={isLoading || rebuild.isPending}
        onClick={() => rebuild.mutate()}
        title={tr(
          '重新计算这篇论文的检索向量（会调用一次嵌入模型）',
          'Recompute this paper’s retrieval vectors (calls the embedding model once)',
        )}
      >
        <Icon
          name="refresh"
          size={11}
          style={rebuild.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
        />
        {rebuild.isPending
          ? tr('构建中…', 'Building…')
          : built
            ? tr('重新构建', 'Rebuild')
            : tr('构建索引', 'Build index')}
      </button>
    </div>
  );
}
