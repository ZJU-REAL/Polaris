const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

function withDefaultProtocol(value) {
  const text = String(value || "").trim();
  if (!text || text.includes("://")) return text;
  const host = text.split(/[/:]/, 1)[0].toLowerCase();
  return `${LOCAL_HOSTS.has(host) ? "http" : "https"}://${text}`;
}

export function normalizePolarisOrigin(value) {
  let url;
  try {
    url = new URL(withDefaultProtocol(value));
  } catch {
    throw new Error("Polaris 地址无效");
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("Polaris 地址必须是安全的 HTTP(S) Origin");
  }
  if (url.protocol === "http:" && !LOCAL_HOSTS.has(url.hostname.toLowerCase())) {
    throw new Error("公网 Polaris 地址必须使用 HTTPS");
  }
  return url.origin;
}

export function normalizeHttpUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(String(value));
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

export function permissionPatternForUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.hostname}/*`;
}

export function originsMatch(left, right) {
  try {
    return normalizePolarisOrigin(left) === normalizePolarisOrigin(right);
  } catch {
    return false;
  }
}
