/* ============================================================
   开场清单（#801）：新用户登录后还差哪几步。

   每一项的「完成」由后端从真实状态算，这里不存进度——存了就必然会和实际
   对不上（删掉唯一的文献库，进度条还停在已完成），而那比不给这张卡更糟。

   文案和落点在这里而不在后端：界面走 tr(zh,en)，后端返回中文串会让中英
   切换对这张卡失效。
   ============================================================ */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { api, type OnboardingItem } from '../../lib/api';
import { tr } from '../../lib/i18n';

/** id → 这一项说什么、点了去哪。落点必须是这个用户真能打开的页面。 */
function copyFor(id: string): { title: string; hint: string; href: string; cta: string } | null {
  switch (id) {
    case 'model':
      return {
        title: tr('添加智能体或模型服务', 'Add an agent or model provider'),
        hint: tr(
          'AI 功能需要它。已装好 Claude Code 等智能体的话，直接添加最快。',
          'AI features need one. If you already use an agent such as Claude Code, adding it is quickest.',
        ),
        href: '/settings?tab=llm',
        cta: tr('添加', 'Add'),
      };
    case 'library':
      return {
        title: tr('新建文献库', 'Create a library'),
        hint: tr(
          '论文都放在文献库里，课题通过关联文献库来使用它们。',
          'Papers live in libraries. Topics use the libraries you link to them.',
        ),
        href: '/libraries',
        cta: tr('新建', 'Create'),
      };
    case 'discipline':
      return {
        title: tr('选择学科', 'Choose a field'),
        hint: tr(
          '决定从论文中提取哪些信息。不限领域的话，保留「通用」即可。',
          'Decides what to extract from papers. If you don’t work in one field, keep General.',
        ),
        href: '/libraries',
        cta: tr('设置', 'Choose'),
      };
    case 'experiment':
      return {
        title: tr('连接实验机器', 'Connect an experiment machine'),
        hint: tr(
          'Python 实验在你通过 SSH 连接的机器上运行。电路、流体等实验在本机运行，不需要。',
          'Python experiments run on a machine you connect over SSH. Circuit and fluid experiments run locally and don’t need one.',
        ),
        href: '/settings?tab=ssh',
        cta: tr('连接', 'Connect'),
      };
    default:
      // 后端加了新项而前端还没跟上：与其显示一个没有文案的空行，不如不显示
      return null;
  }
}

export function OnboardingCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['onboarding'],
    queryFn: () => api.getOnboarding(),
    retry: false,
  });

  const dismiss = useMutation({
    mutationFn: () => api.dismissOnboarding(),
    onSuccess: (next) => queryClient.setQueryData(['onboarding'], next),
  });

  // 读不到就不显示：这张卡是锦上添花，不该因为它自己出错而挡在首页上
  if (!data || data.dismissed || data.done) return null;

  const shown = data.items
    .map((item: OnboardingItem) => ({ item, copy: copyFor(item.id) }))
    .filter((row): row is { item: OnboardingItem; copy: NonNullable<ReturnType<typeof copyFor>> } =>
      row.copy !== null,
    );
  if (shown.length === 0) return null;

  const doneCount = shown.filter((row) => row.item.done).length;

  return (
    <section className="card" style={{ padding: 18, marginBottom: 16 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>
          {tr('开始之前', 'Get set up')}
        </h3>
        <div className="row gap8" style={{ alignItems: 'center' }}>
          <span className="muted" style={{ fontSize: 12 }}>
            {doneCount}/{shown.length}
          </span>
          <button
            className="btn btn-ghost sm"
            onClick={() => dismiss.mutate()}
            disabled={dismiss.isPending}
          >
            {tr('不再提示', 'Dismiss')}
          </button>
        </div>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: '4px 0 14px' }}>
        {tr('完成一项会自动打勾。', 'Items tick off as you finish them.')}
      </p>
      <div className="col gap8">
        {shown.map(({ item, copy }) => (
          <div
            key={item.id}
            className="row gap8"
            style={{ alignItems: 'flex-start', opacity: item.done ? 0.55 : 1 }}
          >
            <span style={{ marginTop: 2, color: item.done ? 'var(--ok)' : 'var(--text-4)' }}>
              <Icon name={item.done ? 'check' : 'dot'} size={14} />
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{copy.title}</div>
              <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>{copy.hint}</div>
            </div>
            {!item.done && (
              <Link className="btn btn-soft sm" to={copy.href}>
                {copy.cta}
              </Link>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
