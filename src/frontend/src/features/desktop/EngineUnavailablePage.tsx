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
    title = tr('安装没有完成', 'Setup didn’t finish');
    detail = tr(
      '请检查网络连接，然后重新打开 Polaris 再试一次。',
      'Check your network connection, then restart Polaris to try again.',
    );
  } else if (problem === 'stale-engine') {
    title = tr('另一个 Polaris 仍在运行', 'Another copy of Polaris is still running');
    detail = tr(
      '请关闭其他 Polaris 窗口后重新打开；仍不行的话，重启电脑再试。',
      'Close any other Polaris windows and restart Polaris. If that doesn’t help, restart your computer.',
    );
  } else if (problem === 'port-in-use') {
    title = tr('端口 18080 被其他程序占用', 'Port 18080 is in use');
    detail = tr(
      'Polaris 需要这个端口，请退出占用它的程序后重新打开 Polaris。',
      'Polaris needs this port. Quit the program using it, then restart Polaris.',
    );
  } else {
    title = tr('本机引擎没有运行', 'The local engine isn’t running');
    detail = tr(
      '重新打开 Polaris 会再次启动它。',
      'Restart Polaris to start it again.',
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
