import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const apiSource = readFileSync(fileURLToPath(new URL('../api.ts', import.meta.url)), 'utf-8');

describe('Polaris extension API key contract', () => {
  it('uses the existing personal key lifecycle endpoints', () => {
    expect(apiSource).toContain("request<DownloadApiKeyCreated>('/me/download-api-key', { method: 'POST' })");
    expect(apiSource).toContain("request<void>('/me/download-api-key', { method: 'DELETE' })");
    expect(apiSource).toContain("request<DownloadClientIdentity>('/download-client/me'");
    expect(apiSource).toContain("'X-Polaris-API-Key': apiKey");
  });

  it('models the one-time plaintext response', () => {
    expect(apiSource).toContain('export interface DownloadApiKeyCreated');
    expect(apiSource).toContain('api_key: string;');
    expect(apiSource).toContain('key_prefix: string;');
    expect(apiSource).toContain('created_at: string;');
  });
});
