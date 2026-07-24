import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import { Segmented } from '../../components/ui/Segmented';
import { useAuth } from '../../app/auth';
import { ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';

type Mode = 'login' | 'register';
type Step = 1 | 2;

const USERNAME_RE = /^[a-z0-9_]{3,32}$/;

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
    if (err.status === 403 && err.message.includes('INVALID_INVITE_CODE')) {
      return tr('邀请码无效', 'Invalid invite code');
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
 * 达标后按字符类别数（小写/大写/数字/符号）与长度加分。
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

export function LoginPage() {
  const navigate = useNavigate();
  const { isAuthenticated, login, register } = useAuth();
  const [mode, setMode] = useState<Mode>('login');
  const [step, setStep] = useState<Step>(1); // 仅注册用：1 账号信息 / 2 设置密码
  const [identifier, setIdentifier] = useState(''); // 登录：邮箱或用户名；注册：邮箱
  const [displayName, setDisplayName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [password2, setPassword2] = useState('');
  const [inviteCode, setInviteCode] = useState('');

  const usernameValid = USERNAME_RE.test(username);
  const pwdLevel = passwordStrength(password);
  const pwdMatch = password2 !== '' && password === password2;

  const mutation = useMutation({
    mutationFn: async () => {
      if (mode === 'login') {
        await login(identifier, password);
      } else {
        await register({
          email: identifier,
          password,
          display_name: displayName.trim(),
          username,
          invite_code: inviteCode,
        });
      }
    },
    onSuccess: () => navigate('/', { replace: true }),
  });

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  const isRegister = mode === 'register';

  function switchMode(m: Mode) {
    setMode(m);
    setStep(1);
    setPassword('');
    setPassword2('');
    mutation.reset();
  }

  function backToStep1() {
    setPassword('');
    setPassword2('');
    mutation.reset();
    setStep(1);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (isRegister && step === 1) {
      if (usernameValid) setStep(2);
      return;
    }
    mutation.mutate();
  }

  return (
    <div className="auth-page">
      {/* 左上角品牌 */}
      <div className="auth-brand">
        <PolarisMark size={56} />
        <PolarisWordmark height={32} />
      </div>

      <div style={{ position: 'absolute', top: 18, right: 20 }}>
        <LangToggle />
      </div>

      {/* 右侧表单卡片 */}
      <div className="auth-card fadeup">
        <div className="auth-card-brandline">{tr('北极星 AI 科研智能体', 'Polaris AI Research Agent')}</div>
        <div className="auth-card-title">
          {isRegister ? tr('创建账号', 'Create your account') : tr('欢迎回来', 'Welcome back')}
        </div>
        <div className="auth-card-sub">
          {isRegister
            ? tr('让 AI 与你一起做研究', 'Do research together with AI')
            : tr('登录以继续你的研究', 'Sign in to continue your research')}
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
                <input
                  id="username"
                  className="auth-input"
                  type="text"
                  required
                  autoComplete="username"
                  placeholder="e.g. zhang_san"
                  value={username}
                  onChange={(e) => setUsername(e.target.value.toLowerCase())}
                />
                <div
                  style={{
                    fontSize: 11,
                    color: username && !usernameValid ? 'var(--danger-tx)' : 'var(--text-4)',
                    marginTop: 4,
                  }}
                >
                  {tr('小写字母、数字、下划线，3-32 位；全局唯一', 'lowercase letters, digits, underscore; 3-32 chars; unique')}
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

          {/* 登录密码 */}
          {!isRegister && (
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
              <button type="button" className="btn" style={{ height: 38 }} onClick={backToStep1}>
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
              disabled={mutation.isPending || (isRegister && !usernameValid)}
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
