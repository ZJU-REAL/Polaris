import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import {
  engineBootstrapStatus,
  kernelLocalBackend,
  type EngineBootstrapStatus,
} from '../../lib/host';
import { tr, useLang } from '../../lib/i18n';
import { BOOTSTRAP_STEPS, bootstrapGate, bootstrapStepIndex } from './engineBootstrap';
import { EngineUnavailablePage } from './EngineUnavailablePage';

/**
 * 桌面端首启等待页（#721）：窗口先起、内核在后台准备本机引擎，这里轮询
 * kernel.engineBootstrapStatus 给用户看得见的进度，而不是几分钟的白屏。
 *
 * 就绪后的放行分两条路：
 * - 探测到本地引擎地址 → 整页 reload。当前文档是引擎起来**之前**加载的，
 *   CSP 里没放行 127.0.0.1，不重载的话对本地引擎的一切请求都会被拦；
 *   重载后 main.tsx 的启动探测直接命中就绪状态，不会再回到这页。
 * - 没有本地引擎 → onProceed() 交给 App，由它显示「本机引擎没有启动」页。
 * 引导失败直接显示同一兜底页的「环境没准备好」版本。
 */
export function EngineBootstrapPage({
  initialStatus,
  onProceed,
}: {
  initialStatus: EngineBootstrapStatus;
  onProceed: () => void;
}) {
  useLang();
  const [status, setStatus] = useState(initialStatus);
  const [leaving, setLeaving] = useState(false);
  const proceededRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const id = window.setInterval(() => {
      void engineBootstrapStatus().then((next) => {
        if (!cancelled && next) setStatus(next);
      });
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const gate = bootstrapGate(status);

  useEffect(() => {
    if (gate !== 'proceed' || proceededRef.current) return;
    proceededRef.current = true;
    setLeaving(true);
    void kernelLocalBackend().then((info) => {
      if (info?.baseUrl) {
        window.location.reload();
      } else {
        onProceed();
      }
    });
  }, [gate, onProceed]);

  const active = bootstrapStepIndex(status.phase);

  if (gate === 'failed') return <EngineUnavailablePage setupFailed />;

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
          {leaving ? tr('马上就好…', 'Almost there…') : tr('正在准备本地环境', 'Preparing the local environment')}
        </div>
        <div className="auth-card-sub">
          {tr('首次启动需要几分钟，之后会快很多。', 'The first launch takes a few minutes; later launches are much faster.')}
        </div>
        <div style={{ display: 'grid', gap: 10, marginTop: 4 }}>
          {BOOTSTRAP_STEPS.map((step, i) => {
            const stepDone = active > i || leaving;
            const stepActive = active === i && !leaving;
            return (
              <div
                key={step.en}
                className="row"
                style={{
                  gap: 8,
                  fontSize: 13,
                  color: stepActive ? 'var(--text-1)' : stepDone ? 'var(--ok-tx)' : 'var(--text-4)',
                  fontWeight: stepActive ? 600 : 400,
                }}
              >
                {stepActive ? (
                  <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                ) : (
                  <Icon name={stepDone ? 'check' : 'dot'} size={13} />
                )}
                {tr(step.zh, step.en)}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
