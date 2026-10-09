import { ApiError } from './api';
import { tr } from './i18n';
import { isLlmNotConfigured, llmNotConfiguredText } from './llmNotConfigured';

/* ============================================================
   接口错误 → 给人看的一句话（「什么没成功 + 怎么办」）。

   ApiError.message 保留后端原样的错误码（不少页面靠 e.message === 'XXX' 分支），
   所以这里不改它，只在要展示的地方调 errorText(e)。
   - 认得的错误码：换成大白话。
   - 不认得的错误码（全大写下划线）：通用一句 + 原码放括号里，方便反馈时对照。
   - 网络不通（fetch 抛 TypeError）：提示检查本机引擎。
   - 其他 Error：原样返回 message（多半已经是人话，如后端透出的第三方报错）。
   表放在函数里：tr() 必须在渲染时求值，不能在模块顶层。
   ============================================================ */

function knownCode(code: string): string | null {
  switch (code) {
    case 'PROJECT_NOT_FOUND':
      return tr('找不到这个课题，可能已被删除', 'This topic no longer exists.');
    case 'PAPER_NOT_FOUND':
      return tr('找不到这篇论文，可能已被删除', 'This paper no longer exists.');
    case 'LIBRARY_NOT_FOUND':
      return tr('找不到这个文献库，可能已被删除', 'This library no longer exists.');
    case 'IDEA_NOT_FOUND':
      return tr('找不到这个想法，可能已被删除', 'This idea no longer exists.');
    case 'EXPERIMENT_NOT_FOUND':
      return tr('找不到这个实验，可能已被删除', 'This experiment no longer exists.');
    case 'VOYAGE_NOT_FOUND':
      return tr('找不到这个任务，可能已被删除', 'This task no longer exists.');
    case 'CONVERSATION_NOT_FOUND':
      return tr('找不到这段对话，可能已被删除', 'This conversation no longer exists.');
    case 'FIGURE_NOT_FOUND':
    case 'ASSET_NOT_FOUND':
    case 'FILE_NOT_FOUND':
    case 'RESOURCE_NOT_FOUND':
    case 'ARTIFACT_NOT_FOUND':
      return tr('找不到这个文件，可能已被删除', 'This file no longer exists.');
    case 'TASK_SERVICE_UNAVAILABLE':
      return tr('本机引擎暂时无法执行任务，请稍后重试', 'The local engine can’t run tasks right now. Try again shortly.');
    case 'DB_MIGRATION_REQUIRED':
      return tr('数据需要升级，请重启 Polaris', 'Your data needs an upgrade. Restart Polaris.');
    case 'IDEA_VOYAGE_ALREADY_RUNNING':
    case 'INGEST_ALREADY_RUNNING':
      return tr('已有一个同类任务在运行，请等它结束', 'A similar task is already running. Wait for it to finish.');
    case 'VOYAGE_ALREADY_FINISHED':
      return tr('任务已经结束', 'This task has already finished.');
    case 'PDF_NOT_AVAILABLE':
      return tr('这篇论文还没有 PDF，请先获取 PDF', 'This paper has no PDF yet. Get the PDF first.');
    case 'PDF_FETCH_FAILED':
      return tr('无法下载 PDF，请稍后重试或手动上传', 'Couldn’t download the PDF. Try again later or upload it.');
    case 'PDF_ALREADY_EXISTS':
      return tr('这篇论文已经有 PDF 了', 'This paper already has a PDF.');
    case 'PDF_UPLOAD_INVALID':
      return tr('文件不是有效的 PDF', 'The file isn’t a valid PDF.');
    case 'EMBEDDING_NOT_AVAILABLE':
      return tr('没有可用的嵌入模型，请在设置中配置', 'No embedding model is available. Set one up in Settings.');
    case 'INDEX_REBUILD_FAILED':
      return tr('索引没有建立成功，请稍后重试', 'Couldn’t build the index. Try again later.');
    case 'COMPILE_REQUIRED':
      return tr('请先编译稿件', 'Compile the manuscript first.');
    case 'COMPILE_FAILED':
      return tr('稿件编译失败，请查看编译日志', 'The manuscript didn’t compile. Check the build log.');
    case 'REVIEW_REQUIRED':
      return tr('请先完成评审', 'Run a review first.');
    case 'ASK_NOT_OPEN':
      return tr('这个问题已经回答过或已关闭', 'This question has already been answered or closed.');
    case 'CHAT_BOT_NOT_CONFIGURED':
      return tr('还没有配置群机器人，请在设置中添加', 'No chat bot is set up. Add one in Settings.');
    case 'FILE_READONLY':
      return tr('这个文件是只读的', 'This file is read-only.');
    case 'SLUG_TAKEN':
      return tr('这个名称已被使用，换一个试试', 'That name is taken. Try another.');
    case 'INTERDISCIPLINARY_SCOPE_REQUIRED':
    case 'INTERDISCIPLINARY_SCOPE_INVALID':
      return tr('请把研究范围填写完整', 'Complete the research scope first.');
    case 'INVALID_IDS':
      return tr('所选内容已变化，请刷新后重试', 'The selection changed. Refresh and try again.');
    default:
      return null;
  }
}

/** 错误码：取 detail 里「CODE」或「CODE:附加信息」的 CODE 部分。 */
function codeOf(message: string): string | null {
  const m = /^([A-Z][A-Z0-9_]{2,})(?::|$)/.exec(message.trim());
  return m?.[1] ?? null;
}

/** 把任意错误变成一句可展示的话。 */
export function errorText(e: unknown): string {
  if (e instanceof TypeError && /fetch|network|load failed/i.test(e.message)) {
    return tr('无法连接本机引擎，请确认 Polaris 正在运行', 'Couldn’t reach the local engine. Check that Polaris is running.');
  }
  const message = e instanceof Error ? e.message : typeof e === 'string' ? e : String(e);
  if (isLlmNotConfigured(message)) return llmNotConfiguredText();
  const code = codeOf(message);
  if (code) {
    const known = knownCode(code);
    if (known) return known;
    return tr(`操作没有完成（${code}）`, `Something went wrong (${code}).`);
  }
  if (e instanceof ApiError && e.status >= 500 && /^(Internal Server Error|HTTP \d+)$/.test(message)) {
    return tr('本机引擎出错了，请稍后重试', 'The local engine hit an error. Try again shortly.');
  }
  return message;
}
