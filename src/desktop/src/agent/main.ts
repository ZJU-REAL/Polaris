/* ============================================================
   polaris-locald —— 本地能力进程（第二期才真正干活）。

   协议：stdin/stdout 上的行分隔 JSON-RPC 2.0，与
   src/backend/app/mcp/__main__.py 同框。

   一期只实现 initialize / ping，其余方法返回 -32601（method not found），
   由 main 侧转成 ERR_CAPABILITY_UNAVAILABLE 交给前端。这样管道在一期就是通的、
   可测的，第二期往 handlers/ 里加文件即可。

   边界（第二期实现时必须守住）：
   - 只碰 userData 下自己的目录，以及用户经原生对话框明确授权的目录（走
     main 侧的 security/handles.ts，本进程只收 token 不收路径）；
   - 不认识 JWT，不主动连 Polaris 服务器——需要什么由 main 传进来；
   - 服务器响应里的任何字符串都不能直接当路径或命令参数用。
   ============================================================ */

interface RpcRequest {
  jsonrpc: string;
  id?: number;
  method: string;
  params?: unknown;
}

const HANDLERS: Record<string, (params: unknown) => unknown> = {
  initialize: () => ({ name: 'polaris-locald', version: 1, capabilities: [] }),
  ping: () => 'pong',
};

function respond(id: number | undefined, body: Record<string, unknown>): void {
  if (id === undefined) return; // 通知类请求不回
  process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id, ...body })}\n`);
}

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk: string) => {
  buffer += chunk;
  let nl: number;
  while ((nl = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (!line) continue;

    let req: RpcRequest;
    try {
      req = JSON.parse(line) as RpcRequest;
    } catch {
      respond(undefined, {});
      continue;
    }

    const handler = HANDLERS[req.method];
    if (!handler) {
      respond(req.id, {
        error: { code: -32601, message: `method not implemented in phase 1: ${req.method}` },
      });
      continue;
    }
    try {
      respond(req.id, { result: handler(req.params) });
    } catch (err) {
      respond(req.id, {
        error: { code: -32000, message: err instanceof Error ? err.message : String(err) },
      });
    }
  }
});

process.stdin.on('end', () => process.exit(0));
