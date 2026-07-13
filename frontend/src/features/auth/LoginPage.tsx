import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { useAuth } from '../../app/auth';
import { ApiError } from '../../lib/api';

type Mode = 'login' | 'register';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400 && err.message.includes('LOGIN_BAD_CREDENTIALS')) {
      return '邮箱或密码错误 Invalid email or password';
    }
    if (err.status === 400 && err.message.includes('REGISTER_USER_ALREADY_EXISTS')) {
      return '该邮箱已注册 Email already registered';
    }
    return `请求失败（${err.status}）：${err.message}`;
  }
  if (err instanceof TypeError) {
    return '无法连接服务器，请确认后端已启动 Cannot reach the API server';
  }
  return err instanceof Error ? err.message : '未知错误 Unknown error';
}

export function LoginPage() {
  const navigate = useNavigate();
  const { isAuthenticated, login, register } = useAuth();
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');

  const mutation = useMutation({
    mutationFn: async () => {
      if (mode === 'login') {
        await login(email, password);
      } else {
        await register({ email, password, invite_code: inviteCode });
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

  return (
    <div className="auth-page">
      <div className="auth-card fadeup">
        {/* 品牌区 */}
        <div className="col" style={{ alignItems: 'center', marginBottom: 24 }}>
          <div className="sb-logo" style={{ width: 44, height: 44, borderRadius: 12, marginBottom: 14 }}>
            <Icon name="sparkle" size={24} style={{ color: '#fff' }} />
          </div>
          <div style={{ fontSize: 21, fontWeight: 700, letterSpacing: '-0.02em' }}>Polaris</div>
          <div style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 5 }}>自动 AI 科研平台</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2, fontFamily: 'var(--mono)' }}>
            Autonomous AI Research Platform
          </div>
        </div>

        <div className="row" style={{ justifyContent: 'center', marginBottom: 22 }}>
          <Segmented<Mode>
            options={[
              { v: 'login', label: '登录 Sign in' },
              { v: 'register', label: '注册 Sign up' },
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
            <label htmlFor="email">
              邮箱<span className="en">Email</span>
            </label>
            <input
              id="email"
              className="auth-input"
              type="email"
              required
              autoComplete="email"
              placeholder="you@zju.edu.cn"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="auth-field">
            <label htmlFor="password">
              密码<span className="en">Password</span>
            </label>
            <input
              id="password"
              className="auth-input"
              type="password"
              required
              minLength={8}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              placeholder="至少 8 位"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {mode === 'register' && (
            <div className="auth-field">
              <label htmlFor="invite">
                邀请码<span className="en">Invite code</span>
              </label>
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
            disabled={mutation.isPending}
            style={{ width: '100%', justifyContent: 'center', height: 38, marginTop: 6 }}
          >
            {mutation.isPending ? (
              <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <Icon name={mode === 'login' ? 'arrow' : 'plus'} size={14} />
            )}
            {mode === 'login' ? '登录 Sign in' : '注册并登录 Sign up'}
          </button>
        </form>

        <div style={{ fontSize: 11, color: 'var(--text-4)', textAlign: 'center', marginTop: 18, lineHeight: 1.5 }}>
          注册需要邀请码 · 如需访问权限请联系管理员
        </div>
      </div>
    </div>
  );
}
