import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import type { LocalEngineProblem } from '../../lib/endpoint';
import { hasHost, hostDiagnostics, onHostEvent, relaunchApp, type EngineFailure } from '../../lib/host';
import { tr, useLang } from '../../lib/i18n';
import { NetworkSettingsForm } from './NetworkSettingsForm';
import { SupportActions } from './SupportActions';

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

export interface UnavailableCopy {
  title: string;
  detail: string;
  /** 这种失败与网络有关：给出代理 / 镜像设置。 */
  network: boolean;
}

/**
 * 该对用户说什么。先看端口问题（最具体、用户自己能解决），再看主进程给的失败
 * 原因，最后才是笼统的说法。以前安装失败一律是「请检查网络连接」——磁盘满了、
 * 安装包不完整、引擎崩了也这么说，用户照做也没用。
 */
export function unavailableCopy(input: {
  setupFailed?: boolean;
  problem?: LocalEngineProblem | null;
  failure?: EngineFailure | null;
}): UnavailableCopy {
  const { setupFailed = false, problem = null, failure = null } = input;
  if (problem === 'stale-engine') {
    return {
      title: tr('另一个 Polaris 仍在运行', 'Another copy of Polaris is still running'),
      detail: tr(
        '请关闭其他 Polaris 窗口后重新打开；仍不行的话，重启电脑再试。',
        'Close any other Polaris windows and restart Polaris. If that doesn’t help, restart your computer.',
      ),
      network: false,
    };
  }
  if (problem === 'port-in-use') {
    return {
      title: tr('端口 18080 被其他程序占用', 'Port 18080 is in use'),
      detail: tr(
        'Polaris 需要这个端口，请退出占用它的程序后重新打开 Polaris。',
        'Polaris needs this port. Quit the program using it, then restart Polaris.',
      ),
      network: false,
    };
  }
  switch (failure?.code) {
    case 'disk-space':
      return {
        title: tr('磁盘空间不足', 'Not enough disk space'),
        detail: tr(
          '安装本机引擎需要约 2 GB 可用空间。清理磁盘后重新打开 Polaris。',
          'Setting up the local engine needs about 2 GB of free space. Free up space, then restart Polaris.',
        ),
        network: false,
      };
    case 'download':
      return {
        title: tr('下载没有完成', 'The download didn’t finish'),
        detail: tr(
          '默认下载源和国内镜像都没有连上。请检查网络或设置代理，然后重新打开 Polaris。',
          'Neither the default sources nor the China mirrors could be reached. Check your network or set a proxy, then restart Polaris.',
        ),
        network: true,
      };
    case 'engine-crashed':
      return {
        title: tr('本机引擎意外停止', 'The local engine stopped unexpectedly'),
        detail: tr(
          '已自动重启一次，但没有恢复。重新打开 Polaris 再试。',
          'It was restarted once and stopped again. Restart Polaris to try again.',
        ),
        network: false,
      };
    case 'engine-start':
      return {
        title: tr('本机引擎没有启动', 'The local engine didn’t start'),
        detail: tr(
          '重新打开 Polaris 会再试一次。问题仍在的话，请把诊断信息发给我们。',
          'Restart Polaris to try again. If it keeps failing, send us the diagnostics.',
        ),
        network: false,
      };
    case 'install':
    case 'resources':
    case 'tree':
      return {
        title: tr('安装没有完成', 'Setup didn’t finish'),
        detail: tr(
          '重新打开 Polaris 会再试一次。问题仍在的话，请把诊断信息发给我们。',
          'Restart Polaris to try again. If it keeps failing, send us the diagnostics.',
        ),
        network: false,
      };
    default:
      break;
  }
  if (setupFailed) {
    return {
      title: tr('安装没有完成', 'Setup didn’t finish'),
      detail: tr(
        '重新打开 Polaris 会再试一次。网络不稳定时可以先设置代理。',
        'Restart Polaris to try again. On an unreliable network, set a proxy first.',
      ),
      network: true,
    };
  }
  return {
    title: tr('本机引擎没有运行', 'The local engine isn’t running'),
    detail: tr('重新打开 Polaris 会再次启动它。', 'Restart Polaris to start it again.'),
    network: false,
  };
}

/**
 * 桌面端拿不到本机引擎时的兜底页：首启环境没装好（setupFailed），或者
 * 引擎没起来 / 中途停了。数据和功能都在本机引擎上，没有它就没有可用的应用——
 * 如实说明原因、给出最后几行输出，并给「重新打开」这条真正会重试的出路，
 * 以及能拿去求助的两样东西：日志文件夹和诊断信息。
 */
export function EngineUnavailablePage({
  setupFailed = false,
  problem = null,
  failure,
  canRelaunch = hasHost(),
  showSupport = hasHost(),
  initialRestarting = false,
  initialShowNetwork = false,
}: {
  setupFailed?: boolean;
  /** 已知原因（#850）：端口被别的程序或残留的旧引擎占着。 */
  problem?: LocalEngineProblem | null;
  /** 主进程给的失败原因。不传（undefined）时自己去问一次；传 null 表示没有。 */
  failure?: EngineFailure | null;
  /** 有桌面宿主才能「重新打开」；没有宿主时这个按钮点了什么也不会发生，干脆不给 */
  canRelaunch?: boolean;
  /** 日志与诊断两个按钮同理：没有宿主就不给 */
  showSupport?: boolean;
  /** 仅测试用：直接画「正在重新打开」的样子 */
  initialRestarting?: boolean;
  /** 仅测试用：网络设置直接展开 */
  initialShowNetwork?: boolean;
}) {
  useLang();
  const [restarting, setRestarting] = useState(initialRestarting);
  const [fetched, setFetched] = useState<EngineFailure | null>(null);
  const [showNetwork, setShowNetwork] = useState(initialShowNetwork);
  const restart = () => runRelaunch(relaunchApp, setRestarting);

  // 调用方没给原因（App 在启动探测后直接落到这页）：问主进程要一次
  useEffect(() => {
    if (failure !== undefined || !hasHost()) return;
    let alive = true;
    void hostDiagnostics().then((diagnostics) => {
      if (alive && diagnostics?.failure) setFetched(diagnostics.failure);
    });
    return () => {
      alive = false;
    };
  }, [failure]);

  // 停在这页时引擎自己回来了（自动重启成功）：重新加载就能进应用
  useEffect(() => {
    return onHostEvent((event) => {
      if (event.type === 'engine.state' && event.state === 'running') window.location.reload();
    });
  }, []);

  const reason = failure ?? fetched;
  const copy = unavailableCopy({ setupFailed, problem, failure: reason });
  const lines = reason?.lines ?? [];

  // 最后几行才是出事的地方：框里默认停在底部
  const linesRef = useRef<HTMLDivElement>(null);
  const linesKey = lines.join('\n');
  useEffect(() => {
    const box = linesRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [linesKey]);

  return (
    <div className="auth-page">
      <div className="auth-brand">
        <PolarisMark size={56} />
        <PolarisWordmark height={32} />
      </div>

      <div className="auth-lang">
        <LangToggle />
      </div>

      <div className="auth-card fadeup" style={lines.length ? { maxWidth: 520 } : undefined}>
        <div className="auth-card-title">{copy.title}</div>
        <div className="auth-card-sub">{copy.detail}</div>
        {lines.length > 0 && (
          <div
            ref={linesRef}
            data-testid="engine-failure-lines"
            className="mono"
            style={{
              margin: '0 0 16px',
              maxHeight: 132,
              overflowY: 'auto',
              overflowX: 'hidden',
              padding: '8px 10px',
              borderRadius: 6,
              background: 'var(--surface-2)',
              border: '0.5px solid var(--border)',
              fontSize: 11,
              lineHeight: 1.5,
              color: 'var(--text-2)',
              whiteSpace: 'pre-wrap',
              overflowWrap: 'anywhere',
              userSelect: 'text',
            }}
          >
            {lines.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        )}
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
        {showSupport && (
          <div style={{ marginTop: 16, paddingTop: 14, borderTop: '0.5px solid var(--border)' }}>
            <SupportActions />
            {copy.network && (
              <div style={{ marginTop: 12 }}>
                <button
                  type="button"
                  onClick={() => setShowNetwork((v) => !v)}
                  aria-expanded={showNetwork}
                  style={{
                    background: 'none',
                    border: 'none',
                    padding: 0,
                    cursor: 'pointer',
                    fontSize: 12,
                    color: 'var(--text-3)',
                  }}
                >
                  {showNetwork ? tr('收起网络设置', 'Hide network settings') : tr('网络设置', 'Network settings')}
                </button>
                {showNetwork && (
                  <div style={{ marginTop: 10 }}>
                    <NetworkSettingsForm />
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
