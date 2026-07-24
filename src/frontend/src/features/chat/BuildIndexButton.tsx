import { useMutation, useQuery } from '@tanstack/react-query';
import { ApiError, api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';

/* ============================================================
   「建立全文索引」按钮：仅当用户在设置里打开了「为论文建立全文索引」
   开关时才显示。点击后异步为对应文献批建全文索引，让文献对话检索更准。
   相关研究对话 / 个人文献库对话共用，只换 build 回调。
   ============================================================ */

/** 建索引端点返回：异步走 {queued, indexable, no_fulltext}，同步库场景走 {indexed, skipped}。 */
type BuildIndexResult = {
  queued?: number;
  indexable?: number;
  no_fulltext?: number;
  indexed?: number;
  skipped?: number;
};

function buildResultToast(data: BuildIndexResult): string {
  // 兼容同步库场景（有 indexed）与异步场景（有 indexable/no_fulltext）
  const indexable = data.indexed ?? data.indexable ?? 0;
  const skipped = data.skipped ?? data.no_fulltext ?? 0;
  if (skipped > 0) {
    return tr(
      `已排队 ${indexable} 篇建立全文索引，${skipped} 篇无全文已跳过（约需几分钟）`,
      `Queued ${indexable} paper(s) for indexing; skipped ${skipped} without full text (takes a few minutes)`,
    );
  }
  return tr(
    `已排队 ${indexable} 篇建立全文索引，完成后对话检索会更准（约需几分钟）`,
    `Queued ${indexable} paper(s) for full-text indexing — chat retrieval will improve once it finishes (takes a few minutes)`,
  );
}

export function BuildIndexButton({ build }: { build: () => Promise<BuildIndexResult> }) {
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });

  const mutation = useMutation({
    mutationFn: build,
    onSuccess: (data) => toast(buildResultToast(data), 'ok'),
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('请先到设置里打开「为论文建立全文索引」', 'Enable the full-text index in Settings first'), 'error');
        return;
      }
      toast(`${tr('操作失败', 'Failed')}：${e instanceof Error ? e.message : String(e)}`, 'error');
    },
  });

  // 未开启开关时不显示按钮
  if (me?.settings?.chat_fulltext_index !== true) return null;

  return (
    <button
      className="btn btn-ghost sm"
      style={{ height: 26, fontSize: 10.5, flexShrink: 0 }}
      title={tr('为这批论文建立全文索引，让对话检索更准', 'Build a full-text index for these papers to improve chat retrieval')}
      disabled={mutation.isPending}
      onClick={() => mutation.mutate()}
    >
      <Icon name="refresh" size={11} style={mutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
      {tr('建立全文索引', 'Build full-text index')}
    </button>
  );
}
