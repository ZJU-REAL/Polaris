/* ============================================================
   目录句柄：renderer 只拿得到不透明 token，永远拿不到也传不了真实路径。

   这样「路径穿越」在协议层就不存在了——本地方法的参数根本不是路径。
   等价思路在后端已经用了两次（latex_compile._safe_relpath、
   ssh_exec._validate_relpath），这里是把它推广到进程边界上。

   一期是空表：没有任何目录被授权，local.fs.pickFolder 也还没实现。
   但结构必须现在建——第二期若先用裸路径上线，再改成 token 就是破坏性协议变更。
   ============================================================ */

import { randomUUID } from 'node:crypto';
import { realpathSync } from 'node:fs';
import { sep } from 'node:path';

interface Grant {
  /** 已 realpath 解析过的绝对路径。 */
  root: string;
  purpose: string;
  /** 只读授权：Polaris 永远不写用户的文献目录（Zotero 库尤其）。 */
  writable: boolean;
}

const grants = new Map<string, Grant>();

/** 登记一个用户明确选中的目录，返回句柄 token。 */
export function grantDirectory(path: string, purpose: string, writable = false): string {
  const root = realpathSync(path);
  const token = randomUUID();
  grants.set(token, { root, purpose, writable });
  return token;
}

export function revokeDirectory(token: string): void {
  grants.delete(token);
}

export function listGrants(): { token: string; purpose: string; writable: boolean }[] {
  return [...grants.entries()].map(([token, g]) => ({
    token,
    purpose: g.purpose,
    writable: g.writable,
  }));
}

/**
 * 把 token + 相对路径解析成绝对路径，越界抛错。
 *
 * 解析后必须再 realpath 一次才比较：否则目录里的软链能指到授权范围之外，
 * 字符串前缀检查会被绕过。
 */
export function resolveWithin(token: string, relative = ''): string {
  const grant = grants.get(token);
  if (!grant) throw new Error('unknown directory handle');
  const candidate = realpathSync(`${grant.root}${sep}${relative}`);
  if (candidate !== grant.root && !candidate.startsWith(grant.root + sep)) {
    throw new Error('path escapes the granted directory');
  }
  return candidate;
}
