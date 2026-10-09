import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import {
  engineBootstrapStatus,
  kernelLocalBackend,
  type EngineBootstrapStatus,
} from '../../lib/host';
import type { LocalEngineProblem } from '../../lib/endpoint';
import { tr, useLang } from '../../lib/i18n';
import {
  BOOTSTRAP_STEPS,
  bootstrapGate,
  bootstrapStepIndex,
  formatBytes,
  formatElapsed,
  isStalled,
} from './engineBootstrap';
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
  initialShowLog = false,
}: {
  initialStatus: EngineBootstrapStatus;
  onProceed: () => void;
  /** 详情框初始是否展开（测试用；默认收起）。 */
  initialShowLog?: boolean;
}) {
  const lang = useLang();
  const [status, setStatus] = useState(initialStatus);
  const [leaving, setLeaving] = useState(false);
  const [showLog, setShowLog] = useState(initialShowLog);
  // 每秒走一格：已用时长与「仍在进行」提示都按它算
  const [now, setNow] = useState(() => Date.now());
  const proceededRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const id = window.setInterval(() => {
      setNow(Date.now());
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

  // 失败时问一次内核有没有已知原因（#850：端口被别的程序/旧引擎占着）。
  // undefined = 还在问，先不渲染，免得「环境没准备好」一闪而过再换成端口提示。
  const [problem, setProblem] = useState<LocalEngineProblem | null | undefined>(undefined);
  useEffect(() => {
    if (gate !== 'failed') return;
    let cancelled = false;
    void kernelLocalBackend()
      .then((info) => info?.problem ?? null)
      .catch(() => null)
      .then((p) => {
        if (!cancelled) setProblem(p);
      });
    return () => {
      cancelled = true;
    };
  }, [gate]);

  const active = bootstrapStepIndex(status.phase);

  // 最近一次看到落盘字节数增长的时刻（宿主只给累计值，增长时刻在这边记）。
  // 按阶段分开算：换阶段字节数从 0 重新累计，不能拿上一阶段的数比较；
  // 每个阶段第一次看到的值只当基线，不算「刚刚在涨」。
  const growthRef = useRef<{ phaseStartedAt?: number; bytes?: number; at?: number }>({});
  {
    const g = growthRef.current;
    if (g.phaseStartedAt !== status.phaseStartedAt) {
      growthRef.current = { phaseStartedAt: status.phaseStartedAt, bytes: status.downloadedBytes };
    } else if (status.downloadedBytes !== undefined && status.downloadedBytes > (g.bytes ?? 0)) {
      growthRef.current = { ...g, bytes: status.downloadedBytes, at: Date.now() };
    }
  }
  const stalled = isStalled(
    { phaseStartedAt: status.phaseStartedAt, lastOutputAt: status.lastOutputAt, lastGrowthAt: growthRef.current.at },
    now,
  );
  const log = status.log ?? [];
  const logKey = log.join('\n');

  const logBoxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const box = logBoxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [logKey, showLog]);

  if (gate === 'failed') {
    if (problem === undefined) return null;
    return problem ? <EngineUnavailablePage problem={problem} /> : <EngineUnavailablePage setupFailed />;
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
              <div key={step.en}>
                <div
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
                {stepActive && (
                  <StepDetail
                    downloadedBytes={status.downloadedBytes}
                    elapsed={status.phaseStartedAt != null ? formatElapsed(now - status.phaseStartedAt, lang) : null}
                    stalled={stalled}
                  />
                )}
              </div>
            );
          })}
        </div>
        {log.length > 0 && !leaving && (
          <div style={{ marginTop: 14 }}>
            <button
              type="button"
              onClick={() => setShowLog((v) => !v)}
              aria-expanded={showLog}
              style={{
                background: 'none',
                border: 'none',
                padding: 0,
                cursor: 'pointer',
                fontSize: 12,
                color: 'var(--text-3)',
              }}
            >
              {showLog ? tr('隐藏详情', 'Hide details') : tr('显示详情', 'Show details')}
            </button>
            {showLog && (
              <div
                ref={logBoxRef}
                data-testid="bootstrap-log"
                className="mono"
                style={{
                  marginTop: 8,
                  maxHeight: 132,
                  overflowY: 'auto',
                  overflowX: 'hidden',
                  padding: '8px 10px',
                  borderRadius: 6,
                  background: 'var(--surface-2)',
                  border: '0.5px solid var(--border)',
                  fontSize: 11,
                  lineHeight: 1.5,
                  color: 'var(--text-3)',
                  whiteSpace: 'pre-wrap',
                  overflowWrap: 'anywhere',
                }}
              >
                {log.map((line, i) => (
                  <div key={i}>{line}</div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 当前步骤下的一行次要信息：已下载多少、这一步用了多久；长时间没动静时
 * 换一句安抚的话。老宿主不给这些字段，整块不渲染。
 */
function StepDetail({
  downloadedBytes,
  elapsed,
  stalled,
}: {
  downloadedBytes?: number;
  elapsed: string | null;
  stalled: boolean;
}) {
  const parts: string[] = [];
  if (downloadedBytes !== undefined && downloadedBytes > 0) {
    const size = formatBytes(downloadedBytes);
    parts.push(tr(`已下载 ${size}`, `${size} downloaded`));
  }
  if (elapsed) parts.push(elapsed);
  if (parts.length === 0 && !stalled) return null;
  return (
    <div
      data-testid="bootstrap-step-detail"
      // 与步骤文字左对齐（图标 13px + 间距 8px）
      style={{ marginTop: 3, paddingLeft: 21, fontSize: 12, lineHeight: 1.5, fontWeight: 400, color: 'var(--text-3)' }}
    >
      {parts.length > 0 && <div style={{ fontVariantNumeric: 'tabular-nums' }}>{parts.join(' · ')}</div>}
      {stalled && <div>{tr('仍在进行，网络较慢时需要更久', 'Still working — this takes longer on a slow network')}</div>}
    </div>
  );
}
