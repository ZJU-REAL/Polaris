import { useMutation } from '@tanstack/react-query';
import { tr } from '../../lib/i18n';
import { errorText } from '../../lib/errors';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';

/* ============================================================
   建立全文索引按钮：点击后异步为对应文献批建全文索引，让文献对话检索更准。
   相关研究对话 / 个人文献库对话共用，只换 build 回调。
   （全文索引不再是可配置项——向量是检索的承重结构，所以按钮恒显示。）
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
      `正在为 ${indexable} 篇建立全文索引，约需几分钟；${skipped} 篇没有全文，已跳过`,
      `Indexing ${indexable} ${indexable === 1 ? 'paper' : 'papers'}, which takes a few minutes. Skipped ${skipped} without full text.`,
    );
  }
  return tr(
    `正在为 ${indexable} 篇建立全文索引，约需几分钟`,
    `Indexing ${indexable} ${indexable === 1 ? 'paper' : 'papers'}, which takes a few minutes.`,
  );
}

export function BuildIndexButton({ build }: { build: () => Promise<BuildIndexResult> }) {
  const mutation = useMutation({
    mutationFn: build,
    onSuccess: (data) => toast(buildResultToast(data), 'ok'),
    onError: (e) => {
      toast(`${tr('无法建立索引：', 'Couldn’t build the index: ')}${errorText(e)}`, 'error');
    },
  });

  // 开关默认开，只有显式关掉才藏起按钮

  return (
    <button
      className="btn btn-ghost sm"
      style={{ height: 26, fontSize: 11, flexShrink: 0 }}
      title={tr('建立全文索引后，回答能引用论文正文', 'With a full-text index, answers can cite the paper body')}
      disabled={mutation.isPending}
      onClick={() => mutation.mutate()}
    >
      <Icon name="refresh" size={11} style={mutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
      {tr('建立全文索引', 'Build full-text index')}
    </button>
  );
}
