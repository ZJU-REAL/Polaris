/* ============================================================
   设置 → 关于：当前版本 + 手动检查更新（#812）。

   更新能力早就有：顶栏的 UpdateBadge 启动 20 秒后自动查一次、之后每 6 小时一次，
   有新版就亮一个箭头。缺的是**入口**——没有新版时它什么都不显示，于是用户既看不到
   自己是哪个版本，也没有办法主动确认「我是不是最新版」。

   手动检查和自动检查的差别在失败时：自动的查失败了安静地当作没有更新，这是对的；
   手动的要是也这样，就会在网络出错、GitHub 限流时告诉用户「已是最新版本」——
   一件没发生的事。所以这里如实区分「已是最新」「没查成」「有新版但安装包还没传完」。
   ============================================================ */
import { useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { UpdateDialog } from '../../components/ui/UpdateDialog';
import { checkUpdate, hostAppVersion, openExternal, type UpdateInfo } from '../../lib/host';
import { tr } from '../../lib/i18n';

const RELEASES_URL = 'https://github.com/ZJU-REAL/Polaris/releases';

export type CheckOutcome =
  | { kind: 'available'; latest: string }
  | { kind: 'latest' }
  | { kind: 'failed'; reason: string }
  | { kind: 'no-newer' };

/**
 * 一次手动检查的结果 → 该对用户说什么。
 *
 * 顺序有讲究：先看 available，再看 error，**最后**才敢说「已是最新」——而且只有
 * 主进程明确说了 checked 才说。老版本外壳的主进程不带 checked 字段，查失败与没有
 * 新版在它那里长得一样，这时只能说「没有发现新版本」，不能打包票。
 */
export function describeCheck(info: UpdateInfo): CheckOutcome {
  if (info.available && info.latestVersion) {
    return { kind: 'available', latest: info.latestVersion };
  }
  if (info.error) {
    return { kind: 'failed', reason: failureReason(info) };
  }
  if (info.checked) {
    return { kind: 'latest' };
  }
  return { kind: 'no-newer' };
}

/**
 * 主进程给的是原因代码，文案在这里配——主进程不知道界面是中文还是英文。
 * 认不出的代码原样带上细节，不编一句假装知道的话。
 */
export function failureReason(info: UpdateInfo): string {
  const detail = info.errorDetail ? `（${info.errorDetail}）` : '';
  switch (info.error) {
    case 'rate-limited':
      return tr(
        'GitHub 暂时限制了查询频率，过一会儿再试',
        'GitHub is rate-limiting update checks — try again in a while',
      );
    case 'network':
      return tr('连不上 GitHub', 'Could not reach GitHub') + detail;
    case 'no-package':
      return tr(
        `v${info.latestVersion ?? '?'} 已发布，但还没有适合本机的安装包，可能仍在上传`,
        `v${info.latestVersion ?? '?'} is out, but there is no package for this system yet — it may still be uploading`,
      );
    case 'http':
      return tr('GitHub 返回了错误', 'GitHub returned an error') + detail;
    default:
      return (info.error ?? '') + detail;
  }
}

export function AboutSettings() {
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<UpdateInfo | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  // 优先用最近一次检查回报的版本：热更新后它才是用户眼前这份界面的版本
  const version = result?.currentVersion ?? hostAppVersion();

  const check = async () => {
    setChecking(true);
    try {
      setResult(await checkUpdate());
    } catch (err) {
      // 桥本身出错也要有个说法：按钮转一圈什么都不显示，比报错更让人困惑
      setResult({
        available: false,
        currentVersion: version ?? '',
        error: 'network',
        errorDetail: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setChecking(false);
    }
  };

  const outcome = result ? describeCheck(result) : null;

  return (
    <section className="card card-pad" style={{ maxWidth: 640 }}>
      <div className="section-h" style={{ marginBottom: 14 }}>
        <Icon name="sparkle" size={15} style={{ color: 'var(--accent)' }} />
        {tr('关于 Polaris', 'About Polaris')}
      </div>

      <div className="row gap12" style={{ alignItems: 'center', marginBottom: 16 }}>
        <span className="muted" style={{ fontSize: 13 }}>{tr('当前版本', 'Current version')}</span>
        <span className="mono" style={{ fontSize: 14, fontWeight: 650 }}>
          {version ? `v${version}` : '—'}
        </span>
      </div>

      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn btn-primary" onClick={() => void check()} disabled={checking}>
          <Icon name="refresh" size={14} />
          {checking ? tr('检查中…', 'Checking…') : tr('检查更新', 'Check for updates')}
        </button>
        <button className="btn btn-ghost" onClick={() => void openExternal(RELEASES_URL)}>
          <Icon name="link" size={14} />
          {tr('所有版本', 'All releases')}
        </button>
      </div>

      {outcome && result && (
        <div style={{ marginTop: 16, fontSize: 13, lineHeight: 1.6 }}>
          {outcome.kind === 'available' && (
            <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
              <span>
                {tr('有新版本', 'A new version is available')}：
                <span className="mono" style={{ fontWeight: 650 }}> v{outcome.latest}</span>
              </span>
              <button className="btn btn-soft sm" onClick={() => setDialogOpen(true)}>
                {tr('查看并更新', 'View and update')}
              </button>
            </div>
          )}
          {outcome.kind === 'latest' && (
            <span style={{ color: 'var(--ok, #2e9e5b)' }}>
              {tr('当前已是最新版本', 'You are on the latest version')}
            </span>
          )}
          {outcome.kind === 'no-newer' && (
            <span className="muted">{tr('没有发现新版本', 'No newer version found')}</span>
          )}
          {outcome.kind === 'failed' && (
            <div className="col gap6">
              <span style={{ color: 'var(--warn, #b7791f)' }}>
                {tr('没能完成检查', 'Could not check for updates')}：{outcome.reason}
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                {tr(
                  '可以直接去发布页看看有没有新版本。',
                  'You can check the releases page directly.',
                )}{' '}
                <a
                  href={result.releaseUrl ?? RELEASES_URL}
                  onClick={(e) => {
                    e.preventDefault();
                    void openExternal(result.releaseUrl ?? RELEASES_URL);
                  }}
                >
                  {tr('打开发布页', 'Open releases')}
                </a>
              </span>
            </div>
          )}
        </div>
      )}

      {result?.available && (
        <UpdateDialog info={result} open={dialogOpen} onClose={() => setDialogOpen(false)} />
      )}
    </section>
  );
}
