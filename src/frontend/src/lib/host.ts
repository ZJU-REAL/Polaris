/* ============================================================
   桌面宿主桥 —— 前端访问 window.polaris 的唯一封装。

   业务代码永远不要直接读 window.polaris：web 端它不存在，直接读会让
   web 构建到处需要判空。这里统一收口，web 端所有调用都是安全的 no-op。

   类型是 src/desktop/src/shared/contract.ts 的镜像，而不是 import 过来的：
   docker/Dockerfile.frontend 只 COPY src/frontend，跨目录 import 会让前端
   镜像构建直接失败。契约以 desktop 侧那份为准，改动需两边同步。
   ============================================================ */

export type ServerProbe =
  | { ok: true; version: string }
  | { ok: false; reason: 'invalid-url' | 'unreachable' | 'timeout' | 'not-polaris'; detail?: string };

export type HostEvent =
  | { type: 'host.serverChanged'; serverUrl: string }
  | { type: 'host.openServerSetup' };

interface HostBridge {
  invoke(method: string, params?: unknown): Promise<unknown>;
  subscribe(handler: (event: HostEvent) => void): number;
  unsubscribe(id: number): void;
}

function bridge(): HostBridge | undefined {
  return typeof window === 'undefined'
    ? undefined
    : (window as unknown as { polaris?: HostBridge }).polaris;
}

/** 是否有可用的桌面宿主桥（比 endpoint.isDesktop() 更严格：桥必须真的注入成功）。 */
export function hasHost(): boolean {
  return bridge() != null;
}

/** 探测服务器地址是否可用；web 端返回 null（该功能只在桌面端有意义）。 */
export async function testServer(url: string): Promise<ServerProbe | null> {
  const b = bridge();
  if (!b) return null;
  return (await b.invoke('host.testServer', { url })) as ServerProbe;
}

/** 保存服务器地址；主进程会随即重建窗口，本调用之后的代码不保证还会执行。 */
export async function setServerUrl(url: string): Promise<void> {
  const b = bridge();
  if (!b) return;
  await b.invoke('host.setServerUrl', { url });
}

/** 用系统浏览器打开外链。 */
export async function openExternal(url: string): Promise<void> {
  await bridge()?.invoke('host.openExternal', { url });
}

/** 兜底剪贴板写入（navigator.clipboard 失败时用）。 */
export async function hostCopyText(text: string): Promise<boolean> {
  const b = bridge();
  if (!b) return false;
  return (await b.invoke('host.copyText', { text })) === true;
}

/** Dock/任务栏角标（待审批数）。 */
export function setBadgeCount(count: number): void {
  void bridge()?.invoke('host.setBadgeCount', { count });
}

/** 订阅宿主事件，返回取消订阅函数（web 端返回 no-op）。 */
export function onHostEvent(handler: (event: HostEvent) => void): () => void {
  const b = bridge();
  if (!b) return () => {};
  const id = b.subscribe(handler);
  return () => b.unsubscribe(id);
}
