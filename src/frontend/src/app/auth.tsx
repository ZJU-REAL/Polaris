import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { getToken, setToken } from '../lib/api';
import { acquireLocalSession } from '../lib/local-session';
import { EngineUnavailablePage } from '../features/desktop/EngineUnavailablePage';

/* 会话（#842）：Polaris 只在本机运行、只有一个用户，没有登录/注册/退出。
   token 只来自本地会话端点；这里只负责持有它。 */
interface AuthValue {
  token: string | null;
  isAuthenticated: boolean;
  /** 采纳一个已按惯例落存储的 token（本地会话引导用）。 */
  adoptToken: (token: string) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken());

  const adoptToken = useCallback((t: string) => {
    setToken(t);
    setTokenState(t);
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ token, isAuthenticated: token !== null, adoptToken }),
    [token, adoptToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>');
  return ctx;
}

/** 路由守卫：还没有会话就静默取本地会话。取的过程中渲染空白（通常一瞬间）；
    引擎连不上就显示带重试的兜底页，而不是一块白屏。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, adoptToken } = useAuth();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (isAuthenticated) return;
    let alive = true;
    void (async () => {
      const token = await acquireLocalSession();
      if (!alive) return;
      if (token) adoptToken(token);
      else setFailed(true);
    })();
    return () => {
      alive = false;
    };
  }, [isAuthenticated, adoptToken]);

  if (isAuthenticated) return <>{children}</>;
  if (failed) return <EngineUnavailablePage />;
  return null;
}
