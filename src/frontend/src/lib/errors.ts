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
      return tr('语义搜索不可用，请在「设置 → 模型」中查看状态', 'Semantic search isn’t available. See Settings → Model.');
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
    // —— 生成想法的前置条件 ——
    case 'IDEA_CONTEXT_EMPTY':
      return tr(
        '没有可用的论文，无法生成想法。请为课题关联有论文的文献库，或先添加论文',
        'There are no papers to work from, so no ideas were generated. Link a library that has papers, or add papers first.',
      );
    case 'LIBRARY_TOO_SMALL':
      return tr(
        '文献库里的论文太少，无法生成想法。请再添加一些论文后重试',
        'The library has too few papers to generate ideas from. Add more papers, then retry.',
      );
    case 'RETRIEVAL_UNAVAILABLE':
      return tr(
        '语义检索暂时不可用，请稍后重试；如果一直不行，请在「设置 → 模型」中查看状态',
        'Semantic search isn’t available right now. Retry shortly, or check Settings → Model if it keeps failing.',
      );
    // —— 任务因出错暂停的原因（voyage 的 last_error.code，见后端 agents/voyage/failures.py）——
    case 'NETWORK':
    case 'LLM_UNREACHABLE':
      return tr('网络连接失败，请检查网络后重试', 'The network connection failed. Check your connection, then retry.');
    case 'TIMEOUT':
      return tr('请求超时，请稍后重试', 'The request timed out. Retry shortly.');
    case 'RATE_LIMITED':
    case 'LLM_RATE_LIMITED':
      return tr('请求太频繁，被暂时限流，请稍后重试', 'Too many requests, so the service rate-limited this one. Retry shortly.');
    case 'UPSTREAM_UNAVAILABLE':
    case 'LLM_OVERLOADED':
      return tr('对方服务暂时不可用，请稍后重试', 'The service is temporarily unavailable. Retry shortly.');
    case 'DB_BUSY':
      return tr('本地数据正忙，请稍后重试', 'Local data is busy. Retry shortly.');
    case 'AUTH':
    case 'LLM_AUTH':
      return tr(
        '对方服务拒绝了访问，请检查密钥或登录状态后重试',
        'The service refused access. Check the key or sign-in, then retry.',
      );
    case 'BAD_REQUEST':
    case 'LLM_BAD_REQUEST':
      return tr(
        '对方服务不接受这次请求，重试多半没有用，请查看原始报错',
        'The service rejected this request. Retrying is unlikely to help. See the raw error.',
      );
    case 'NOT_CONFIGURED':
      return llmNotConfiguredText();
    case 'REFUSAL':
    case 'LLM_REFUSED':
      return tr('模型拒绝了这次请求，请调整内容后重试', 'The model declined this request. Change the content, then retry.');
    case 'INVALID_OUTPUT':
    case 'LLM_SCHEMA':
      return tr('拿到的结果不符合要求，重试通常可以解决', 'The result wasn’t in the expected form. Retrying usually fixes it.');
    case 'LLM_TRUNCATED':
      return tr('模型的回答太长，没有写完，请重试', 'The model’s answer was cut off before it finished. Retry.');
    case 'LLM_ERROR':
      return tr('模型调用失败，请稍后重试', 'The model call failed. Retry shortly.');
    case 'MISSING_PREREQUISITE':
      return tr(
        '缺少运行所需的文件或组件，请查看原始报错',
        'A file or component this task needs is missing. See the raw error.',
      );
    case 'INTERNAL':
      return tr(
        '程序内部出错，请重试；反复出现时请带上原始报错反馈',
        'Polaris hit an internal error. Retry, and report the raw error if it keeps happening.',
      );
    case 'STEP_FAILED':
      return tr('这一步没有执行成功，请查看原始报错后重试', 'This step didn’t succeed. See the raw error, then retry.');
    case 'CHECK_FAILED':
      return tr(
        '这一步的结果没有通过检查，重试会重新执行这一步',
        'This step’s result didn’t pass its check. Retrying runs the step again.',
      );
    case 'NEEDS_USER':
      return tr('需要你来决定下一步，请查看任务后重试或取消', 'This needs your decision. Review the task, then retry or cancel it.');
    case 'NO_PROGRESS':
      return tr(
        '多次自动恢复后仍然没有进展，请查看出错的步骤后重试或取消',
        'Several automatic recoveries made no progress. Review the failed step, then retry or cancel.',
      );
    default:
      return null;
  }
}

/** 错误码 → 一句话；不认得返回 null（任务的 last_error.code 等不走 errorText 的地方用）。 */
export function errorCodeText(code: string): string | null {
  return knownCode(code);
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
