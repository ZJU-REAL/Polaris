import { useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import { hasHost, relaunchApp } from '../../lib/host';
import { tr, useLang } from '../../lib/i18n';

/** 点「重新打开」：真重启了页面就没了；调用返回或失败还停在这儿，说明没成，按钮得能再点。 */
export async function runRelaunch(
  relaunch: () => Promise<void>,
  setRestarting: (v: boolean) => void,
): Promise<void> {
  setRestarting(true);
  try {
    await relaunch();
  } catch {
    /* 宿主没接住：下面照样复位 */
  } finally {
    setRestarting(false);
  }
}

/**
 * 桌面端拿不到本机引擎时的兜底页：首启环境没装好（setupFailed），或者
 * 引擎没起来。数据和功能都在本机引擎上，没有它就没有可用的应用——如实
 * 说明，并给「重新打开」这条真正会重试的出路，而不是留一块白屏。
 */
export function EngineUnavailablePage({
  setupFailed = false,
  canRelaunch = hasHost(),
  initialRestarting = false,
}: {
  setupFailed?: boolean;
  /** 有桌面宿主才能「重新打开」；没有宿主时这个按钮点了什么也不会发生，干脆不给 */
  canRelaunch?: boolean;
  /** 仅测试用：直接画「正在重新打开」的样子 */
  initialRestarting?: boolean;
}) {
  useLang();
  const [restarting, setRestarting] = useState(initialRestarting);
  const restart = () => runRelaunch(relaunchApp, setRestarting);

  return (
    <div className="auth-page">
      <div className="auth-brand">
        <PolarisMark size={56} />
        <PolarisWordmark height={32} />
      </div>

      <div className="auth-lang">
        <LangToggle />
      </div>

      <div className="auth-card fadeup">
        <div className="auth-card-title">
          {setupFailed
            ? tr('本机环境没有准备好', 'Local setup did not finish')
            : tr('本机引擎没有启动', 'The local engine is not running')}
        </div>
        <div className="auth-card-sub">
          {setupFailed
            ? tr(
                '准备本机运行环境时出了问题。重新打开 Polaris 会再试一次；如果反复失败，请检查网络后再试。',
                'Something went wrong while preparing the local environment. Restarting Polaris will try again; if it keeps failing, check your network connection.',
              )
            : tr(
                'Polaris 的数据和功能都由这台电脑上的引擎提供，它现在没有运行。重新打开 Polaris 会再启动一次。',
                'Polaris keeps your data and runs its features on an engine on this computer, and that engine is not running. Restarting Polaris will start it again.',
              )}
        </div>
        <div className="row" style={{ gap: 10, marginTop: 6 }}>
          {/* 「再检查一次」任何时候都能点：重开卡住了也还有这条路 */}
          <button
            type="button"
            className={canRelaunch ? 'btn' : 'btn btn-primary'}
            style={canRelaunch ? { height: 38 } : { flex: 1, justifyContent: 'center', height: 38 }}
            onClick={() => window.location.reload()}
          >
            <Icon name="refresh" size={14} />
            {tr('再检查一次', 'Check again')}
          </button>
          {canRelaunch && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={restarting}
              style={{ flex: 1, justifyContent: 'center', height: 38 }}
              onClick={() => void restart()}
            >
              <Icon name="arrow" size={14} />
              {restarting ? tr('正在重新打开…', 'Restarting…') : tr('重新打开 Polaris', 'Restart Polaris')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
