import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import { Segmented } from '../../components/ui/Segmented';
import { useAuth } from '../../app/auth';
import { ApiError, api } from '../../lib/api';
import { getLastAccount, isRemembered } from '../../lib/token-store';
import { tr } from '../../lib/i18n';

type Mode = 'login' | 'register';
type View = 'auth' | 'forgot';
type Step = 1 | 2;

const USERNAME_RE = /^[a-z0-9_]{3,32}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400 && err.message.includes('LOGIN_BAD_CREDENTIALS')) {
      return tr('账号或密码错误', 'Invalid account or password');
    }
    if (err.status === 400 && err.message.includes('REGISTER_USER_ALREADY_EXISTS')) {
      return tr('该邮箱已注册', 'Email already registered');
    }
    if (err.status === 400 && err.message.includes('USERNAME_TAKEN')) {
      return tr('用户名已被占用，换一个吧', 'Username already taken');
    }
    if (err.status === 400 && err.message.includes('INVALID_CODE')) {
      return tr('验证码错误或已过期，请重新获取', 'Invalid or expired code — request a new one');
    }
    if (err.status === 400 && err.message.includes('PASSWORD_')) {
      return tr('密码不符合要求：至少 8 位且含字母和数字', 'Password must be 8+ chars with letters and digits');
    }
    if (err.status === 403 && err.message.includes('INVALID_INVITE_CODE')) {
      return tr('邀请码无效', 'Invalid invite code');
    }
    if (err.status === 503 && err.message.includes('EMAIL_NOT_CONFIGURED')) {
      return tr('服务器未配置邮件系统，请联系管理员', 'Email is not configured on the server — contact an admin');
    }
    return `${tr('请求失败', 'Request failed')}（${err.status}）：${err.message}`;
  }
  if (err instanceof TypeError) {
    return tr('无法连接服务器，请确认后端已启动', 'Cannot reach the API server — make sure the backend is running');
  }
  return err instanceof Error ? err.message : tr('未知错误', 'Unknown error');
}

/**
 * 密码强度：0 = 未达最低要求（≥8 位且同时含字母和数字），1 弱 / 2 中 / 3 强。
 * 达标后按字符类别数（小写/大写/数字/符号）与长度加分。后端 validate_password
 * 用的是同一条达标线。
 */
function passwordStrength(pw: string): 0 | 1 | 2 | 3 {
  if (pw.length < 8 || !/[a-zA-Z]/.test(pw) || !/\d/.test(pw)) return 0;
  let score = [/[a-z]/, /[A-Z]/, /\d/, /[^a-zA-Z0-9]/].filter((re) => re.test(pw)).length;
  if (pw.length >= 12) score += 1;
  return score <= 2 ? 1 : score === 3 ? 2 : 3;
}

const STRENGTH_LABEL: Record<1 | 2 | 3, { zh: string; en: string }> = {
  1: { zh: '弱', en: 'Weak' },
  2: { zh: '中', en: 'Fair' },
  3: { zh: '强', en: 'Strong' },
};

function PasswordMeter({ password }: { password: string }) {
  const level = passwordStrength(password);
  return (
    <>
      <div className={`pwd-meter lv${level}`}>
        <div className="pwd-meter-bars">
          <i />
          <i />
          <i />
        </div>
        <span className="pwd-meter-label">
          {level === 0 ? '' : tr(STRENGTH_LABEL[level].zh, STRENGTH_LABEL[level].en)}
        </span>
      </div>
      <div
        className="pwd-hint"
        style={{ color: password && level === 0 ? 'var(--danger-tx)' : 'var(--text-4)' }}
      >
        {tr('至少 8 位，需同时包含字母和数字', 'At least 8 characters, with both letters and digits')}
      </div>
    </>
  );
}

/** 验证码发送 + 冷却倒计时。冷却由后端裁决（retry_after），前端只做展示。 */
function useCodeSender(purpose: 'register' | 'reset') {
  const [cooldown, setCooldown] = useState(0);
  const [note, setNote] = useState('');
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (cooldown <= 0) return;
    timer.current = window.setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => window.clearTimeout(timer.current);
  }, [cooldown]);

  const mutation = useMutation({
    mutationFn: (email: string) => api.sendAuthCode(email, purpose),
    onSuccess: (r) => {
      setCooldown(r.sent ? 60 : r.retry_after);
      setNote(
        r.sent
          ? tr('验证码已发送，请查收邮件', 'Code sent — check your inbox')
          : tr('发送太频繁，请稍后再试', 'Too many requests — try again shortly'),
      );
    },
  });

  return { cooldown, note, send: mutation.mutate, sending: mutation.isPending, error: mutation.error };
}

/** 验证码输入框 + 右侧「获取验证码」按钮。 */
function CodeField({
  id,
  code,
  onCode,
  email,
  sender,
}: {
  id: string;
  code: string;
  onCode: (v: string) => void;
  email: string;
  sender: ReturnType<typeof useCodeSender>;
}) {
  const emailOk = EMAIL_RE.test(email.trim());
  return (
    <div className="auth-field">
      <label htmlFor={id}>{tr('邮箱验证码', 'Email code')}</label>
      <div className="auth-code-row">
        <input
          id={id}
          className="auth-input"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          required
          maxLength={6}
          placeholder={tr('6 位验证码', '6-digit code')}
          value={code}
          onChange={(e) => onCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
        />
        <button
          type="button"
          className="btn"
          disabled={!emailOk || sender.cooldown > 0 || sender.sending}
          onClick={() => sender.send(email.trim())}
        >
          {sender.cooldown > 0
            ? `${sender.cooldown}s`
            : sender.sending
              ? tr('发送中', 'Sending')
              : tr('获取验证码', 'Send code')}
        </button>
      </div>
      {(sender.note || sender.error) && (
        <div
          style={{
            fontSize: 11,
            marginTop: 4,
            color: sender.error ? 'var(--danger-tx)' : 'var(--text-4)',
          }}
        >
          {sender.error ? errorMessage(sender.error) : sender.note}
        </div>
      )}
      {!emailOk && (
        <div style={{ fontSize: 11, marginTop: 4, color: 'var(--text-4)' }}>
          {tr('先填写上方邮箱，再获取验证码', 'Enter your email above first')}
        </div>
      )}
    </div>
  );
}

export function LoginPage() {
  const navigate = useNavigate();
  const { isAuthenticated, login, register } = useAuth();
  const [view, setView] = useState<View>('auth');
  const [mode, setMode] = useState<Mode>('login');
  const [step, setStep] = useState<Step>(1); // 仅注册用：1 账号信息 / 2 设置密码
  // 记住上次登录的账号（只记账号，不记密码；写入在 auth.tsx 的 login 里）
  const [identifier, setIdentifier] = useState(() => getLastAccount());
  const [displayName, setDisplayName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [password2, setPassword2] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [emailCode, setEmailCode] = useState('');
  const [remember, setRemember] = useState(() => isRemembered());

  // 忘记密码
  const [forgotEmail, setForgotEmail] = useState('');
  const [forgotCode, setForgotCode] = useState('');
  const [forgotDone, setForgotDone] = useState(false);

  const capabilities = useQuery({
    queryKey: ['auth-capabilities'],
    queryFn: () => api.authCapabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const emailOn = capabilities.data?.email ?? false;

  const registerSender = useCodeSender('register');
  const resetSender = useCodeSender('reset');

  const usernameValid = USERNAME_RE.test(username);
  const pwdLevel = passwordStrength(password);
  const pwdMatch = password2 !== '' && password === password2;

  // 用户名可用性：停止输入 400ms 后查询（格式合法才查）
  const [usernameDebounced, setUsernameDebounced] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setUsernameDebounced(username), 400);
    return () => clearTimeout(t);
  }, [username]);
  const usernameCheck = useQuery({
    queryKey: ['username-available', usernameDebounced],
    queryFn: () => api.usernameAvailable(usernameDebounced),
    enabled: mode === 'register' && USERNAME_RE.test(usernameDebounced),
    staleTime: 30_000,
    retry: false,
  });
  const checkSettled = usernameValid && usernameDebounced === username && usernameCheck.data != null;
  const usernameTaken = checkSettled && !usernameCheck.data!.available;
  const usernameFree = checkSettled && usernameCheck.data!.available;

  const mutation = useMutation({
    mutationFn: async () => {
      if (mode === 'login') {
        await login(identifier, password, remember);
      } else {
        await register({
          email: identifier,
          password,
          display_name: displayName.trim(),
          username,
          invite_code: inviteCode,
          email_code: emailCode,
        });
      }
    },
    onSuccess: () => navigate('/', { replace: true }),
  });

  const resetMutation = useMutation({
    mutationFn: () => api.resetPassword(forgotEmail.trim(), forgotCode, password),
    onSuccess: () => setForgotDone(true),
  });

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  const isRegister = mode === 'register';

  function clearPasswords() {
    setPassword('');
    setPassword2('');
  }

  function switchMode(m: Mode) {
    setMode(m);
    setStep(1);
    clearPasswords();
    setEmailCode('');
    mutation.reset();
  }

  function toLogin() {
    setView('auth');
    setMode('login');
    setStep(1);
    clearPasswords();
    setForgotCode('');
    setForgotDone(false);
    resetMutation.reset();
    mutation.reset();
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (isRegister && step === 1) {
      if (usernameValid && !usernameTaken) setStep(2);
      return;
    }
    mutation.mutate();
  }

  function onForgotSubmit(e: FormEvent) {
    e.preventDefault();
    resetMutation.mutate();
  }

  const brand = (
    <>
      <div className="auth-brand">
        <PolarisMark size={56} />
        <PolarisWordmark height={32} />
      </div>
      <div className="auth-lang">
        <LangToggle />
      </div>
    </>
  );

  /* ---------- 忘记密码 ---------- */
  if (view === 'forgot') {
    return (
      <div className="auth-page">
        {brand}
        <div className="auth-card fadeup">
          <div className="auth-card-title">{tr('找回密码', 'Reset password')}</div>
          <div className="auth-card-sub">
            {forgotDone
              ? tr('密码已重设，用新密码登录吧', 'Password updated — sign in with it')
              : tr('用注册邮箱接收验证码，然后设置新密码', 'Get a code by email, then set a new password')}
          </div>

          {forgotDone ? (
            <button
              type="button"
              className="btn btn-primary"
              style={{ width: '100%', justifyContent: 'center', height: 38 }}
              onClick={toLogin}
            >
              <Icon name="arrow" size={14} />
              {tr('去登录', 'Go to sign in')}
            </button>
          ) : (
            <form onSubmit={onForgotSubmit}>
              {resetMutation.isError && (
                <div className="auth-error">{errorMessage(resetMutation.error)}</div>
              )}

              <div className="auth-field">
                <label htmlFor="forgot-email">{tr('注册邮箱', 'Account email')}</label>
                <input
                  id="forgot-email"
                  className="auth-input"
                  type="email"
                  required
                  autoComplete="email"
                  placeholder="you@zju.edu.cn"
                  value={forgotEmail}
                  onChange={(e) => setForgotEmail(e.target.value)}
                />
              </div>

              <CodeField
                id="forgot-code"
                code={forgotCode}
                onCode={setForgotCode}
                email={forgotEmail}
                sender={resetSender}
              />

              <div className="auth-field">
                <label htmlFor="new-password">{tr('新密码', 'New password')}</label>
                <input
                  id="new-password"
                  className="auth-input"
                  type="password"
                  required
                  autoComplete="new-password"
                  placeholder={tr('至少 8 位，含字母和数字', 'At least 8 chars, letters and digits')}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <PasswordMeter password={password} />
              </div>

              <div className="auth-field">
                <label htmlFor="new-password2">{tr('确认新密码', 'Confirm new password')}</label>
                <input
                  id="new-password2"
                  className="auth-input"
                  type="password"
                  required
                  autoComplete="new-password"
                  placeholder={tr('再输入一遍', 'Type it again')}
                  value={password2}
                  onChange={(e) => setPassword2(e.target.value)}
                />
                {password2 !== '' && !pwdMatch && (
                  <div style={{ fontSize: 11, color: 'var(--danger-tx)', marginTop: 4 }}>
                    {tr('两次输入的密码不一致', 'Passwords do not match')}
                  </div>
                )}
              </div>

              <div className="row" style={{ gap: 10, marginTop: 6 }}>
                <button type="button" className="btn" style={{ height: 38 }} onClick={toLogin}>
                  {tr('返回登录', 'Back')}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={
                    resetMutation.isPending ||
                    forgotCode.length < 6 ||
                    pwdLevel === 0 ||
                    !pwdMatch
                  }
                  style={{ flex: 1, justifyContent: 'center', height: 38 }}
                >
                  {resetMutation.isPending ? (
                    <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  ) : (
                    <Icon name="check" size={14} />
                  )}
                  {tr('重设密码', 'Reset password')}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    );
  }

  /* ---------- 登录 / 注册 ---------- */
  return (
    <div className="auth-page">
      {brand}

      <div className="auth-card fadeup">
        <div className="auth-card-title">
          {isRegister ? tr('创建账号', 'Create your account') : tr('欢迎回来', 'Welcome back')}
        </div>
        <div className="auth-card-sub">
          {tr('北极星 AI 科研智能体，让 AI 与你一起做研究', 'Polaris AI research agent — do research together with AI')}
        </div>

        <div className="row" style={{ justifyContent: 'center', marginBottom: 18 }}>
          <Segmented<Mode>
            options={[
              { v: 'login', label: tr('登录', 'Sign in') },
              { v: 'register', label: tr('注册', 'Sign up') },
            ]}
            value={mode}
            onChange={switchMode}
          />
        </div>

        {isRegister && (
          <div className="auth-steps">
            <div className={`auth-step${step === 1 ? ' active' : ' done'}`}>
              <span className="dot">{step > 1 ? <Icon name="check" size={11} /> : '1'}</span>
              {tr('账号信息', 'Account')}
            </div>
            <div className="auth-step-line" />
            <div className={`auth-step${step === 2 ? ' active' : ''}`}>
              <span className="dot">2</span>
              {tr('设置密码', 'Password')}
            </div>
          </div>
        )}

        {mutation.isError && <div className="auth-error">{errorMessage(mutation.error)}</div>}

        <form onSubmit={onSubmit}>
          {/* 登录表单 / 注册第 1 步：账号信息 */}
          {(!isRegister || step === 1) && (
            <div className="auth-field">
              <label htmlFor="identifier">
                {isRegister ? tr('邮箱', 'Email') : tr('邮箱或用户名', 'Email or username')}
              </label>
              <input
                id="identifier"
                className="auth-input"
                type={isRegister ? 'email' : 'text'}
                required
                autoComplete={isRegister ? 'email' : 'username'}
                placeholder={isRegister ? 'you@zju.edu.cn' : tr('邮箱或用户名', 'Email or username')}
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
              />
            </div>
          )}

          {isRegister && step === 1 && (
            <>
              {emailOn && (
                <CodeField
                  id="register-code"
                  code={emailCode}
                  onCode={setEmailCode}
                  email={identifier}
                  sender={registerSender}
                />
              )}
              <div className="auth-field">
                <label htmlFor="displayName">{tr('姓名', 'Name')}</label>
                <input
                  id="displayName"
                  className="auth-input"
                  type="text"
                  required
                  maxLength={255}
                  autoComplete="name"
                  placeholder={tr('你的真实姓名', 'Your name')}
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                />
              </div>
              <div className="auth-field">
                <label htmlFor="username">{tr('用户名', 'Username')}</label>
                <div style={{ position: 'relative' }}>
                  <input
                    id="username"
                    className="auth-input"
                    type="text"
                    required
                    autoComplete="username"
                    placeholder="e.g. zhang_san"
                    style={{ width: '100%' }}
                    value={username}
                    onChange={(e) => setUsername(e.target.value.toLowerCase())}
                  />
                  {usernameFree && (
                    <Icon
                      name="check"
                      size={15}
                      style={{
                        position: 'absolute',
                        right: 12,
                        top: '50%',
                        transform: 'translateY(-50%)',
                        color: 'var(--ok)',
                      }}
                    />
                  )}
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: usernameTaken || (username && !usernameValid) ? 'var(--danger-tx)' : 'var(--text-4)',
                    marginTop: 4,
                  }}
                >
                  {usernameTaken
                    ? tr('用户名已存在，换一个吧', 'Username already taken — try another')
                    : tr('小写字母、数字、下划线，3-32 位；全局唯一', 'lowercase letters, digits, underscore; 3-32 chars; unique')}
                </div>
              </div>
              <div className="auth-field">
                <label htmlFor="invite">{tr('邀请码', 'Invite code')}</label>
                <input
                  id="invite"
                  className="auth-input"
                  type="text"
                  required
                  placeholder="POLARIS-XXXX"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value)}
                />
              </div>
            </>
          )}

          {/* 登录密码 + 记住密码 / 忘记密码 */}
          {!isRegister && (
            <>
              <div className="auth-field">
                <label htmlFor="password">{tr('密码', 'Password')}</label>
                <input
                  id="password"
                  className="auth-input"
                  type="password"
                  required
                  autoComplete="current-password"
                  placeholder={tr('输入密码', 'Enter password')}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
              <div className="auth-options">
                <label className="auth-check">
                  <input
                    type="checkbox"
                    checked={remember}
                    onChange={(e) => setRemember(e.target.checked)}
                  />
                  {tr('记住密码', 'Remember me')}
                </label>
                {capabilities.data?.password_reset && (
                  <button
                    type="button"
                    className="auth-link"
                    onClick={() => {
                      setForgotEmail(identifier.includes('@') ? identifier : '');
                      clearPasswords();
                      setView('forgot');
                    }}
                  >
                    {tr('忘记密码？', 'Forgot password?')}
                  </button>
                )}
              </div>
            </>
          )}

          {/* 注册第 2 步：设置密码 */}
          {isRegister && step === 2 && (
            <>
              <div className="auth-step2-account">
                {tr('账号：', 'Account: ')}
                <b>{identifier}</b>
              </div>
              <div className="auth-field">
                <label htmlFor="password">{tr('密码', 'Password')}</label>
                <input
                  id="password"
                  className="auth-input"
                  type="password"
                  required
                  autoFocus
                  autoComplete="new-password"
                  placeholder={tr('至少 8 位，含字母和数字', 'At least 8 chars, letters and digits')}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <PasswordMeter password={password} />
              </div>
              <div className="auth-field">
                <label htmlFor="password2">{tr('确认密码', 'Confirm password')}</label>
                <input
                  id="password2"
                  className="auth-input"
                  type="password"
                  required
                  autoComplete="new-password"
                  placeholder={tr('再输入一遍', 'Type it again')}
                  value={password2}
                  onChange={(e) => setPassword2(e.target.value)}
                />
                {password2 !== '' && !pwdMatch && (
                  <div style={{ fontSize: 11, color: 'var(--danger-tx)', marginTop: 4 }}>
                    {tr('两次输入的密码不一致', 'Passwords do not match')}
                  </div>
                )}
              </div>
            </>
          )}

          {/* 操作按钮 */}
          {isRegister && step === 2 ? (
            <div className="row" style={{ gap: 10, marginTop: 6 }}>
              <button
                type="button"
                className="btn"
                style={{ height: 38 }}
                onClick={() => {
                  clearPasswords();
                  mutation.reset();
                  setStep(1);
                }}
              >
                {tr('上一步', 'Back')}
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={mutation.isPending || pwdLevel === 0 || !pwdMatch}
                style={{ flex: 1, justifyContent: 'center', height: 38 }}
              >
                {mutation.isPending ? (
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                ) : (
                  <Icon name="plus" size={14} />
                )}
                {tr('注册并登录', 'Sign up and log in')}
              </button>
            </div>
          ) : (
            <button
              type="submit"
              className="btn btn-primary"
              disabled={
                mutation.isPending ||
                (isRegister && (!usernameValid || usernameTaken || (emailOn && emailCode.length < 6)))
              }
              style={{ width: '100%', justifyContent: 'center', height: 38, marginTop: 6 }}
            >
              {mutation.isPending ? (
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <Icon name="arrow" size={14} />
              )}
              {isRegister ? tr('下一步', 'Next') : tr('登录', 'Sign in')}
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
