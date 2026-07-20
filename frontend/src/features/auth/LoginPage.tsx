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

export function LoginPage() {
  const navigate = useNavigate();
  const { isAuthenticated, login, register } = useAuth();
  const [mode, setMode] = useState<Mode>('login');
  const [identifier, setIdentifier] = useState(''); // 登录：邮箱或用户名；注册：邮箱
  const [displayName, setDisplayName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');

  const usernameValid = USERNAME_RE.test(username);

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

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    mutation.mutate();
  }

  const isRegister = mode === 'register';

  return (
    <div className="auth-page">
      <div style={{ position: 'absolute', top: 18, right: 20 }}>
        <LangToggle />
      </div>
      <div className="auth-card fadeup">
        {/* 品牌区 */}
        <div className="col" style={{ alignItems: 'center', marginBottom: 24 }}>
          <div style={{ marginBottom: 14 }}>
            <PolarisMark size={72} />
          </div>
          <PolarisWordmark height={28} />
          <div style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 5 }}>
            {tr('自动 AI 科研平台', 'Autonomous AI Research Platform')}
          </div>
        </div>

        <div className="row" style={{ justifyContent: 'center', marginBottom: 22 }}>
          <Segmented<Mode>
            options={[
              { v: 'login', label: tr('登录', 'Sign in') },
              { v: 'register', label: tr('注册', 'Sign up') },
            ]}
            value={mode}
            onChange={(m) => {
              setMode(m);
              mutation.reset();
            }}
          />
        </div>

        {mutation.isError && <div className="auth-error">{errorMessage(mutation.error)}</div>}

        <form onSubmit={onSubmit}>
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

          {isRegister && (
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
            </>
          )}

          <div className="auth-field">
            <label htmlFor="password">{tr('密码', 'Password')}</label>
            <input
              id="password"
              className="auth-input"
              type="password"
              required
              minLength={8}
              autoComplete={isRegister ? 'new-password' : 'current-password'}
              placeholder={tr('至少 8 位', 'At least 8 characters')}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {isRegister && (
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
          )}

          <button
            type="submit"
            className="btn btn-primary"
            disabled={mutation.isPending || (isRegister && !usernameValid)}
            style={{ width: '100%', justifyContent: 'center', height: 38, marginTop: 6 }}
          >
            {mutation.isPending ? (
              <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <Icon name={isRegister ? 'plus' : 'arrow'} size={14} />
            )}
            {isRegister ? tr('注册并登录', 'Sign up and log in') : tr('登录', 'Sign in')}
          </button>
        </form>

        <div style={{ fontSize: 11, color: 'var(--text-4)', textAlign: 'center', marginTop: 18, lineHeight: 1.5 }}>
          {tr('注册需要邀请码 · 如需访问权限请联系管理员', 'Registration requires an invite code · contact the admin for access')}
        </div>
      </div>
    </div>
  );
}
