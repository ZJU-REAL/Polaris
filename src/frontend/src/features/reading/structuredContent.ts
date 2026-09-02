import { apiResourceUrl } from '../../lib/api';

/** Convert signed same-origin resource links for Electron's remote server origin. */
export function resolveStructuredResourceUrls(markdown: string): string {
  return markdown.replace(
    /([("'])(\/api\/structured-content-assets\/[A-Za-z0-9._~-]+)/g,
    (_match, prefix: string, url: string) => `${prefix}${apiResourceUrl(url)}`,
  );
}
