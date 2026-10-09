import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* ============================================================
   #850：只有「会话失效」的 401 才重取会话并刷新页面；别的 401（填错的浏览器
   扩展 key 等）要原样报给用户。整页刷新 10 秒内最多一次，防止刷新死循环。
   ============================================================ */

const handleUnauthorized = vi.fn();
vi.mock('../local-session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../local-session')>();
  return { ...actual, handleUnauthorized: () => handleUnauthorized() };
});

const { RELOAD_GUARD_MS, claimReload, isSessionExpiry } = await import('../local-session');
const { api, ApiError } = await import('../api');

class MemoryStorage {
  private data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

describe('isSessionExpiry', () => {
  it('treats the auth layer rejecting the bearer token as an expired session', () => {
    expect(isSessionExpiry(401, 'Unauthorized')).toBe(true);
    expect(isSessionExpiry(401, 'DOWNLOAD_AUTH_REQUIRED')).toBe(true);
    expect(isSessionExpiry(401, null)).toBe(true);
  });

  it('leaves endpoint-specific 401s and other statuses alone', () => {
    expect(isSessionExpiry(401, 'DOWNLOAD_API_KEY_INVALID')).toBe(false);
    expect(isSessionExpiry(401, 'DOWNLOAD_API_KEY_EXPIRED')).toBe(false);
    expect(isSessionExpiry(401, 'INVALID_REGISTRATION_TOKEN')).toBe(false);
    expect(isSessionExpiry(403, 'Unauthorized')).toBe(false);
    expect(isSessionExpiry(500, null)).toBe(false);
  });

  it('never blames the session for a request authenticated by an extension key', () => {
    const headers = new Headers({ 'X-Polaris-API-Key': 'pol_dl_x' });
    expect(isSessionExpiry(401, 'Unauthorized', headers)).toBe(false);
    expect(isSessionExpiry(401, null, headers)).toBe(false);
  });
});

describe('claimReload', () => {
  it('allows at most one reload per guard window', () => {
    const storage = new MemoryStorage();
    const t0 = 1_000_000;
    expect(claimReload(t0, storage)).toBe(true);
    expect(claimReload(t0 + 1, storage)).toBe(false);
    expect(claimReload(t0 + RELOAD_GUARD_MS - 1, storage)).toBe(false);
    expect(claimReload(t0 + RELOAD_GUARD_MS, storage)).toBe(true);
    expect(claimReload(t0 + RELOAD_GUARD_MS + 5, storage)).toBe(false);
  });

  it('does not lock reloads forever when the clock went backwards', () => {
    const storage = new MemoryStorage();
    expect(claimReload(5_000_000, storage)).toBe(true);
    expect(claimReload(1_000, storage)).toBe(true);
  });

  it('falls back to allowing the reload when storage is unavailable', () => {
    expect(claimReload(1, null)).toBe(true);
    const broken = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => undefined,
    };
    expect(claimReload(1, broken)).toBe(true);
  });
});

describe('api 401 handling', () => {
  beforeEach(() => {
    handleUnauthorized.mockReset();
    vi.stubGlobal('localStorage', new MemoryStorage());
    vi.stubGlobal('sessionStorage', new MemoryStorage());
    localStorage.setItem('polaris.token', 'session-token');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function respond(status: number, body: unknown): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })),
    );
  }

  it('surfaces an invalid extension key instead of reloading the app', async () => {
    respond(401, { detail: 'DOWNLOAD_API_KEY_INVALID' });
    const err = await api.testDownloadApiKey('pol_dl_wrong').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as InstanceType<typeof ApiError>).status).toBe(401);
    expect((err as InstanceType<typeof ApiError>).message).toBe('DOWNLOAD_API_KEY_INVALID');
    expect(handleUnauthorized).not.toHaveBeenCalled();
    // 会话 token 原样留着
    expect(localStorage.getItem('polaris.token')).toBe('session-token');
  });

  it('recovers the session when the bearer token itself is rejected', async () => {
    respond(401, { detail: 'Unauthorized' });
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(handleUnauthorized).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('polaris.token')).toBeNull();
  });
});
