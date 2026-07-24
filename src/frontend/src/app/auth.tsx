import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api, getToken, setToken, type RegisterInput } from '../lib/api';

interface AuthValue {
  token: string | null;
  isAuthenticated: boolean;
  /** 登录成功后 token 写入 localStorage。 */
  login: (email: string, password: string) => Promise<void>;
  /** 注册（含邀请码）后自动登录。 */
  register: (input: RegisterInput) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const queryClient = useQueryClient();

  const login = useCallback(
    async (email: string, password: string) => {
      const t = await api.login(email, password);
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

  const logout = useCallback(() => {
    setToken(null);
    setTokenState(null);
    // 登出：清掉缓存，避免下一个登录用户短暂看到上一个用户的数据
    queryClient.clear();
  }, [queryClient]);

  const value = useMemo<AuthValue>(
    () => ({ token, isAuthenticated: token !== null, login, register, logout }),
    [token, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>');
  return ctx;
}

/** 路由守卫：未登录跳转 /login。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  const location = useLocation();
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}
