import { useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import type { LocalEngineProblem } from '../../lib/endpoint';
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
  problem = null,
  canRelaunch = hasHost(),
  initialRestarting = false,
}: {
  setupFailed?: boolean;
  /** 已知原因（#850）：端口被别的程序或残留的旧引擎占着。 */
  problem?: LocalEngineProblem | null;
  /** 有桌面宿主才能「重新打开」；没有宿主时这个按钮点了什么也不会发生，干脆不给 */
  canRelaunch?: boolean;
  /** 仅测试用：直接画「正在重新打开」的样子 */
  initialRestarting?: boolean;
}) {
  useLang();
  const [restarting, setRestarting] = useState(initialRestarting);
  const restart = () => runRelaunch(relaunchApp, setRestarting);

  let title: string;
  let detail: string;
  if (setupFailed) {
    title = tr('本机环境没有准备好', 'Local setup did not finish');
    detail = tr(
      '准备本机运行环境时出了问题。重新打开 Polaris 会再试一次；如果反复失败，请检查网络后再试。',
      'Something went wrong while preparing the local environment. Restarting Polaris will try again; if it keeps failing, check your network connection.',
    );
  } else if (problem === 'stale-engine') {
    title = tr('还有一个旧的 Polaris 引擎在运行', 'An older Polaris engine is still running');
    detail = tr(
      '端口 18080 被另一个 Polaris 引擎占着（可能是上次没有正常退出，或者同时开着另一份 Polaris）。请关掉其他 Polaris 窗口；仍然不行的话，在活动监视器/任务管理器里结束名为 python 的 Polaris 引擎进程，或重启电脑，然后重新打开 Polaris。',
      'Port 18080 is held by another Polaris engine (it may not have shut down last time, or another copy of Polaris is open). Close other Polaris windows; if that does not help, end the Polaris engine (a python process) in Activity Monitor / Task Manager, or restart your computer, then reopen Polaris.',
    );
  } else if (problem === 'port-in-use') {
    title = tr('端口 18080 被别的程序占用', 'Port 18080 is in use by another program');
    detail = tr(
      'Polaris 的本机引擎需要用 18080 端口，但它现在被另一个程序占着。请退出那个程序后重新打开 Polaris。',
      'The Polaris engine needs port 18080 on this computer, but another program is using it. Quit that program, then restart Polaris.',
    );
  } else {
    title = tr('本机引擎没有启动', 'The local engine is not running');
    detail = tr(
      'Polaris 的数据和功能都由这台电脑上的引擎提供，它现在没有运行。重新打开 Polaris 会再启动一次。',
      'Polaris keeps your data and runs its features on an engine on this computer, and that engine is not running. Restarting Polaris will start it again.',
    );
  }

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
        <div className="auth-card-title">{title}</div>
        <div className="auth-card-sub">{detail}</div>
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
