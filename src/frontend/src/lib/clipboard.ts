/* 复制文本：三级回退，返回是否成功。

   桌面端页面跑在 app://polaris（secure context），navigator.clipboard 本来就可用，
   所以这主要是加固而非救急——但窗口失焦时 writeText 会以 NotAllowedError 拒绝，
   而站内有两处调用没有任何保护（复制邀请链接 / 复制协作链接），会抛未捕获异常。 */

import { hostCopyText } from './host';

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 失焦 / 权限被拒 → 继续往下回退 */
  }

  try {
    if (await hostCopyText(text)) return true;
  } catch {
    /* 桌面桥不可用 → 继续往下回退 */
  }

  // 最后兜底：非 secure context 的老路径
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
