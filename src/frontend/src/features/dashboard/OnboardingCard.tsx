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
        title: tr('配置大模型', 'Set up a model'),
        hint: tr(
          '没有可用的模型路由时，AI 功能会直接报 LLM_NOT_CONFIGURED。',
          'Without a usable model route, AI features fail with LLM_NOT_CONFIGURED.',
        ),
        href: '/settings?tab=llm',
        cta: tr('去配置', 'Configure'),
      };
    case 'library':
      return {
        title: tr('建一个文献库', 'Create a library'),
        hint: tr(
          '文献库是语料的来源——课题本身不持有论文，它读的是所关联文献库的并集。',
          'Libraries hold the corpus; a topic owns no papers of its own, it reads the libraries linked to it.',
        ),
        href: '/libraries',
        cta: tr('去新建', 'Create'),
      };
    case 'discipline':
      return {
        title: tr('选择学科口径', 'Pick a discipline'),
        hint: tr(
          '决定论文按哪套字段抽取。研究不分领域的话，通用口径就是对的答案，可以直接收起这份清单。',
          'Sets which fields papers are extracted into. If your work is not field-specific, the general fields are the right answer — just dismiss this list.',
        ),
        href: '/libraries',
        cta: tr('去设置', 'Set it'),
      };
    case 'experiment':
      return {
        title: tr('连一台跑实验的机器', 'Connect a machine'),
        hint: tr(
          '通用的 Python 实验需要一台 SSH 机器；电路、流体等专用后端在本机容器里跑，不需要。',
          'General Python experiments need an SSH machine. The circuit and fluid backends run in local containers and do not.',
        ),
        href: '/settings?tab=ssh',
        cta: tr('去连接', 'Connect'),
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
        <h3 style={{ fontSize: 14, fontWeight: 700, margin: 0 }}>
          {tr('先把这几件事配好', 'A few things to set up first')}
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
        {tr(
          '每一项都按你账号的实际状态判断，配好了自动打勾。',
          'Each item reflects your account’s actual state and ticks itself once done.',
        )}
      </p>
      <div className="col gap8">
        {shown.map(({ item, copy }) => (
          <div
            key={item.id}
            className="row gap8"
            style={{ alignItems: 'flex-start', opacity: item.done ? 0.55 : 1 }}
          >
            <span style={{ marginTop: 2, color: item.done ? 'var(--ok, #2e9e5b)' : 'var(--muted)' }}>
              <Icon name={item.done ? 'check' : 'dot'} size={14} />
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{copy.title}</div>
              <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>{copy.hint}</div>
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
