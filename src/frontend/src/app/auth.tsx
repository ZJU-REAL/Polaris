import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api, getToken, setToken, type RegisterInput } from '../lib/api';
import { acquireLocalSession, detectLocalSession } from '../lib/local-session';
import { setLastAccount, setRemembered } from '../lib/token-store';

interface AuthValue {
  token: string | null;
  isAuthenticated: boolean;
  /** 登录成功后写入 token；remember 决定落 localStorage 还是 sessionStorage。 */
  login: (email: string, password: string, remember?: boolean) => Promise<void>;
  /** 注册（含邀请码）后自动登录。 */
  register: (input: RegisterInput) => Promise<void>;
  logout: () => void;
  /** 采纳一个已按惯例落存储的 token（desktop 免登录引导用），不清缓存。 */
  adoptToken: (token: string) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const queryClient = useQueryClient();

  const login = useCallback(
    async (email: string, password: string, remember = true) => {
      const t = await api.login(email, password);
      // 先定偏好再写 token：writeToken 据此决定落哪个 storage
      setRemembered(remember);
      setLastAccount(remember ? email : null);
      setToken(t);
      setTokenState(t);
      // 切换账号：清空所有缓存查询，避免残留上一个用户的 me/项目/文献库等数据
      queryClient.clear();
    },
    [queryClient],
  );

  const register = useCallback(
    async (input: RegisterInput) => {
      await api.register(input);
      await login(input.email, input.password);
    },
    [login],
  );

  const adoptToken = useCallback((t: string) => {
    setToken(t);
    setTokenState(t);
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setTokenState(null);
    // 登出：清掉缓存，避免下一个登录用户短暂看到上一个用户的数据
    queryClient.clear();
  }, [queryClient]);

  const value = useMemo<AuthValue>(
    () => ({ token, isAuthenticated: token !== null, login, register, logout, adoptToken }),
    [token, login, register, logout, adoptToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>');
  return ctx;
}

/** 路由守卫：未登录先探测 desktop 免登录能力——支持就静默取会话直接进应用
    （用户永远不见登录页），否则回落到 /login。探测期间渲染空白，避免闪现登录页。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, adoptToken } = useAuth();
  const location = useLocation();
  // checking = 正在探测/取会话；login = 免登录不可用，走现有登录页流程
  const [fallback, setFallback] = useState<'checking' | 'login'>('checking');

  useEffect(() => {
    if (isAuthenticated) return;
    let alive = true;
    void (async () => {
      if (await detectLocalSession()) {
        const token = await acquireLocalSession();
        if (token) {
          if (alive) adoptToken(token);
          return;
        }
      }
      if (alive) setFallback('login');
    })();
    return () => {
      alive = false;
    };
  }, [isAuthenticated, adoptToken]);

  if (isAuthenticated) return <>{children}</>;
  if (fallback === 'login') {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return null;
}
