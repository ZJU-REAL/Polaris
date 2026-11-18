/* ============================================================
   设置 → 关于：当前版本 + 手动检查更新（#812）。

   更新能力早就有：顶栏的 UpdateBadge 启动 20 秒后自动查一次、之后每 6 小时一次，
   有新版就亮一个箭头。缺的是**入口**——没有新版时它什么都不显示，于是用户既看不到
   自己是哪个版本，也没有办法主动确认「我是不是最新版」。

   手动检查和自动检查的差别在失败时：自动的查失败了安静地当作没有更新，这是对的；
   手动的要是也这样，就会在网络出错、GitHub 限流时告诉用户「已是最新版本」——
   一件没发生的事。所以这里如实区分「已是最新」「没查成」「有新版但安装包还没传完」。

   版本有两个：应用（安装包）和本机引擎。两者平时相同，但只换界面的更新不动引擎，
   反馈问题时两个都要——所以两个都写在这里，也都进「复制诊断信息」。
   这一页同时是出问题时的落脚处：打开日志文件夹、复制诊断信息、网络设置。
   ============================================================ */
import { useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { UpdateDialog } from '../../components/ui/UpdateDialog';
import { NetworkSettingsForm } from '../desktop/NetworkSettingsForm';
import { SupportActions } from '../desktop/SupportActions';
import {
  checkUpdate,
  hostAppVersion,
  hostDiagnostics,
  openExternal,
  type Diagnostics,
  type UpdateInfo,
} from '../../lib/host';
import { tr } from '../../lib/i18n';
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack } from './settingsUi';

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
      return tr('查询太频繁，请稍后再试', 'Too many checks. Try again later.');
    case 'network':
      return tr('无法连接 GitHub', 'Couldn’t reach GitHub') + detail;
    case 'no-package':
      return tr(
        `v${info.latestVersion ?? '?'} 已发布，适合本机的安装包还没准备好，请稍后再试`,
        `v${info.latestVersion ?? '?'} is out, but the installer for this computer isn’t ready yet. Try again later.`,
      );
    case 'http':
      return tr('GitHub 返回错误', 'GitHub returned an error') + detail;
    default:
      return (info.error ?? '') + detail;
  }
}

export interface VersionLines {
  /** 应用版本，如 v0.6.0；界面单独更新过时带上界面版本。 */
  app: string;
  /** 本机引擎版本；没在运行时是一句说明而不是版本号。 */
  engine: string;
  engineRunning: boolean;
}

/**
 * 版本两行该写什么。诊断信息还没取到（或老外壳取不到）时，应用版本退回界面
 * 自己知道的那个，引擎写「—」——不知道就不写，不编一个。
 */
export function versionLines(
  diagnostics: Pick<Diagnostics, 'appVersion' | 'uiVersion' | 'engineVersion'> | null,
  fallbackVersion: string | null,
): VersionLines {
  if (!diagnostics) {
    return { app: fallbackVersion ? `v${fallbackVersion}` : '—', engine: '—', engineRunning: false };
  }
  const app =
    diagnostics.uiVersion && diagnostics.uiVersion !== diagnostics.appVersion
      ? `v${diagnostics.appVersion}${tr(`（界面 v${diagnostics.uiVersion}）`, ` (interface v${diagnostics.uiVersion})`)}`
      : `v${diagnostics.appVersion}`;
  if (!diagnostics.engineVersion) {
    return { app, engine: tr('未运行', 'Not running'), engineRunning: false };
  }
  // 从源码跑的引擎自报 dev，不是一个版本号
  const engine = /^\d/.test(diagnostics.engineVersion) ? `v${diagnostics.engineVersion}` : diagnostics.engineVersion;
  return { app, engine, engineRunning: true };
}

export function AboutSettings() {
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<UpdateInfo | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  // 优先用最近一次检查回报的版本：热更新后它才是用户眼前这份界面的版本
  const version = result?.currentVersion ?? hostAppVersion();

  useEffect(() => {
    let alive = true;
    void hostDiagnostics().then((next) => {
      if (alive) setDiagnostics(next);
    });
    return () => {
      alive = false;
    };
  }, []);
  const versions = versionLines(diagnostics, version);

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
    <SettingsStack>
      <SettingsSection title={tr('关于 Polaris', 'About Polaris')}>
        <SettingsGroup>
          <SettingsRow label={tr('版本', 'Version')}>
            <span className="mono" style={{ fontSize: 13 }}>{versions.app}</span>
          </SettingsRow>
          <SettingsRow label={tr('本机引擎', 'Local engine')}>
            <span
              className={versions.engineRunning ? 'mono' : undefined}
              style={{ fontSize: 13, color: versions.engineRunning ? undefined : 'var(--text-3)' }}
            >
              {versions.engine}
            </span>
          </SettingsRow>
          <SettingsRow
            label={tr('更新', 'Updates')}
            hint={outcome && result ? (
              outcome.kind === 'available' ? (
                <span>
                  {tr('有新版本', 'New version')}：<span className="mono">v{outcome.latest}</span>
                  {result.kind === 'full' && result.fullReason === 'engine' && (
                    <>
                      {' · '}
                      {tr('本机引擎也会更新，需要重新安装', 'The local engine changes too, so this one needs the installer')}
                    </>
                  )}
                </span>
              ) : outcome.kind === 'latest' ? (
                <span style={{ color: 'var(--ok-tx)' }}>{tr('已是最新版本', 'You’re up to date')}</span>
              ) : outcome.kind === 'no-newer' ? (
                tr('没有发现新版本', 'No newer version found')
              ) : (
                <span style={{ color: 'var(--warn-tx)' }}>
                  {tr('检查失败', 'Couldn’t check for updates')}：{outcome.reason}{' '}
                  <a
                    href={result.releaseUrl ?? RELEASES_URL}
                    onClick={(e) => {
                      e.preventDefault();
                      void openExternal(result.releaseUrl ?? RELEASES_URL);
                    }}
                  >
                    {tr('在发布页查看', 'Check the releases page')}
                  </a>
                </span>
              )
            ) : undefined}
          >
            {outcome?.kind === 'available' ? (
              <button className="btn btn-primary sm" onClick={() => setDialogOpen(true)}>
                {tr('更新', 'Update')}
              </button>
            ) : (
              <button className="btn btn-ghost sm" onClick={() => void check()} disabled={checking}>
                {checking ? tr('检查中…', 'Checking…') : tr('检查更新', 'Check for updates')}
              </button>
            )}
          </SettingsRow>
          <SettingsRow label={tr('所有版本', 'All releases')}>
            <button className="btn btn-ghost sm" onClick={() => void openExternal(RELEASES_URL)}>
              <Icon name="link" size={13} />
              GitHub
            </button>
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('问题排查', 'Troubleshooting')}
        desc={tr('反馈问题时，请附上诊断信息。', 'When you report a problem, include the diagnostics.')}
      >
        <SettingsGroup>
          <SettingsRow
            label={tr('日志与诊断信息', 'Logs and diagnostics')}
            hint={tr('诊断信息包含版本、系统和最近的引擎日志，不含密钥。', 'Diagnostics list versions, your system and recent engine logs. No keys.')}
            stack
          >
            <SupportActions />
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('网络', 'Network')}
        desc={tr('下载本机引擎组件和检索文献时使用。', 'Used when downloading local engine components and searching literature.')}
      >
        <SettingsGroup pad>
          <NetworkSettingsForm />
        </SettingsGroup>
      </SettingsSection>

      {result?.available && (
        <UpdateDialog info={result} open={dialogOpen} onClose={() => setDialogOpen(false)} />
      )}
    </SettingsStack>
  );
}
