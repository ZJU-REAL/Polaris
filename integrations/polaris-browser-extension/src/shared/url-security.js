const PRIVATE_IPV4 = [
  /^10\./,
  /^127\./,
  /^169\.254\./,
  /^192\.168\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^0\./,
];
const SENSITIVE_QUERY_KEY = /^(?:access_?key|api_?key|token|key|secret|signature|sig|auth|authorization|cdk|x-amz-(?:security-token|signature|credential))$/i;

export function isLoopbackHost(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  return host === "localhost" || host === "::1" || PRIVATE_IPV4.some((pattern) => pattern.test(host));
}

export function isPrivateOrLocalHost(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  return isLoopbackHost(host) || host.endsWith(".local") || host.endsWith(".internal") || host === "0.0.0.0";
}

export function parseSafeHttpUrl(value, { allowRelative = false } = {}) {
  const text = String(value || "").trim();
  if (!text) return null;
  if (allowRelative && text.startsWith("/") && !text.startsWith("//")) return text;
  let parsed;
  try {
    parsed = new URL(text);
  } catch {
    return null;
  }
  if (parsed.username || parsed.password) return null;
  if (!["http:", "https:"].includes(parsed.protocol)) return null;
  return parsed.toString();
}

export function isAllowedYfrOrigin(origin) {
  try {
    const url = new URL(origin);
    if (url.protocol === "https:" && url.hostname === "yfr.yangy.cn") return true;
    return url.protocol === "http:" && isLoopbackHost(url.hostname);
  } catch {
    return false;
  }
}

export function isTrustedYfrPdfAssetUrl(value, expectedOrigin) {
  try {
    const url = new URL(value, expectedOrigin);
    const accessKeys = url.searchParams.getAll("accessKey");
    return isAllowedYfrOrigin(url.origin)
      && url.origin === expectedOrigin
      && /^\/api\/daily-review\/assets\/pdfs\/[^/]+\.pdf$/i.test(url.pathname)
      && accessKeys.length === 1
      && accessKeys[0].length > 0
      && accessKeys[0].length <= 512;
  } catch {
    return false;
  }
}

export function sanitizeImportedHttpUrl(value, {
  expectedYfrOrigin,
  preserveYfrPdfAccessKey = false,
} = {}) {
  const safe = parseSafeHttpUrl(value, { allowRelative: true });
  if (!safe) return null;
  let url;
  try {
    url = new URL(safe, expectedYfrOrigin);
  } catch {
    return null;
  }
  const preserveAccessKey = preserveYfrPdfAccessKey
    && isTrustedYfrPdfAssetUrl(url.toString(), expectedYfrOrigin);
  for (const key of Array.from(url.searchParams.keys())) {
    if (SENSITIVE_QUERY_KEY.test(key) && !(preserveAccessKey && key === "accessKey")) {
      url.searchParams.delete(key);
    }
  }
  return url.toString();
}

export function hasSensitiveUrlParameters(value) {
  try {
    return Array.from(new URL(value).searchParams.keys()).some((key) => SENSITIVE_QUERY_KEY.test(key));
  } catch {
    return false;
  }
}

export function redactSensitiveUrl(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`;
  } catch {
    return "";
  }
}

export function redactSensitiveText(value) {
  return String(value || "")
    .replace(/([?&](?:access_?key|api_?key|token|key|secret|signature|sig|auth|authorization|cdk|x-amz-(?:security-token|signature|credential))=[^&#\s"'<>}\]),]*)/gi, "[redacted]");
}
