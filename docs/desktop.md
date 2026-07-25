# 桌面端（Electron 外壳）

对标 Codex app / Claude Desktop 的形态：**外壳 + 本地进程**，重状态（Postgres/pgvector、
Redis、ARQ worker、LLM 调用）全部留在远端服务器。**不做完整单机离线版**。

代码在 `src/desktop/`，与 `src/frontend`、`src/backend` 平级，有独立的 `package.json`。

## 为什么是 Electron 而不是 Tauri

需要 macOS + Windows + Linux 三平台，而 Tauri 在 Linux 走 WebKitGTK，在 NVIDIA 卡上有
已知的空白窗口与渲染故障。Polaris 的界面恰好是 pdf.js canvas + CodeMirror 6 + KaTeX + yjs
这种重渲染组合，风险正好撞在一起。代价（安装包 85–150MB、空闲内存约 168MB）在内部
自用场景可接受。

## 进程分层

```
Renderer（src/frontend 的现有 React 代码，sandbox:true）
  ↓ 单通道 ipcRenderer.invoke('polaris:rpc') / .on('polaris:event')
Main（外壳与仲裁层：窗口/菜单/协议/配置，不做业务重活）
  ↓ 行分隔 JSON-RPC over stdio（第二期才真正干活）
polaris-locald（child_process，Node）：本地编译 / 目录扫描 / 缓存
  
远端服务器（api / worker / postgres / redis）——由 Renderer 直连
```

**Renderer 直连服务器，主进程不做 API 代理。** 一旦代理，SSE 流式转发、WebSocket 升级、
blob 流、token 语义都要在主进程里重写一遍，等于把 `lib/api.ts`、`lib/sse.ts`、`lib/ws.ts`
再实现一次。桌面壳的价值是「加能力」，不是「接管网络」。

## 页面加载：`app://polaris`

不用 `file://`：Chromium 把它当 opaque origin，`new Worker('file:///...')` 会被拒绝，
而 pdf.js 阅读器依赖 worker，PDF 阅读页会完全不可用；且 `/assets/`、`/pdfjs/cmaps/`
这些绝对路径全要改，桌面端产物会与 web 分叉。

也不用本机 HTTP server：本机任意进程都能访问那个端口，凭空多一个网络攻击面。

注册为 `standard + secure` 之后页面有真正的 origin，pushState / Worker / localStorage /
clipboard / Notification 全部按 https 页面的规则工作，**前端一行结构代码都不用改**
（`vite.config.ts` 的 `base` 保持默认、`createBrowserRouter` 保持不动）。

`polaris://` 是另一件事——留给深链接，不要和内容协议混用。

## IPC 契约

`src/desktop/src/shared/contract.ts` 是唯一真相：一个方法表 + 一个事件联合类型。

单通道而不是每个能力一个 `ipcMain.handle`：preload 是打进安装包、renderer 直接可见的
边界，按能力开通道的话，第二期每加一个本地能力都要同时改 preload + main + renderer。
现在 preload 写完就不再改，加方法只动 contract 与 main 侧实现。

方法命名 `<域>.<对象>.<动词>`：`host.*` 是外壳能力，`local.*` 留给第二期的本地计算。

本地 agent 用 `child_process` 而不是 Electron 的 `utilityProcess`：后者不支持 stdin 管道，
而「行分隔 JSON-RPC over stdio、将来可以原地换成一个 Python 进程」正是这层的意义所在
（framing 与 `src/backend/app/mcp/__main__.py` 一致）。Node 侧靠 `ELECTRON_RUN_AS_NODE`
复用 Electron 自带运行时——打包后的应用里没有独立的 `node`。

前端所有「走本地还是走远端」的判断只读能力清单（`host.capabilities`），**不要读 platform、
不要读版本号做特判**。本地实现经 `lib/local-routes.ts` 注册，只有 `LocalUnavailable`
才静默回落服务器：本地跑出的业务错误（如 LaTeX 语法错）是正常结果，不该再去服务器跑一遍。

## 几个不能动的地方

| 位置 | 约束 |
|---|---|
| `window.ts` 的 `plugins: true` | 写作页的论文预览是 `<iframe src=blob:…pdf>`，走 Chromium 内置 pdfium 而不是 pdf.js。Electron 默认关插件，去掉这一项预览会全白 |
| CSP 的 `script-src 'wasm-unsafe-eval'` | pdf.js 的 openjpeg / qcms 是 WASM |
| CSP 的 `style-src 'unsafe-inline'` | CodeMirror 的 style-mod 与 KaTeX 在运行时 insertRule 注入样式，去掉编辑器和公式渲染会崩 |
| `menu.ts` 的 `role: 'editMenu'` | macOS 上不注册它，Cmd+C/V/A/Z 会在部分控件里失效 |
| 换服务器后重建窗口 | preload 注入值与 CSP 响应头都在文档加载时定下，`reload()` 刷不掉 |

## 前端侧的约定

- 服务器地址只经 `src/frontend/src/lib/endpoint.ts`（`apiBase()` / `wsUrl()` / `portalUrl()`），
  组件里不要拼 `window.location`。web 端这些函数全部退化为原来的相对路径行为。
- 桌面能力只经 `src/frontend/src/lib/host.ts`，web 端是安全的 no-op。**不要在组件里直接读
  `window.polaris`**，否则 web 构建到处需要判空。
- 分享链接用 `portalUrl()` 而不是 `window.location.origin`：这些链接是给别人用浏览器打开的，
  桌面端必须指向服务器上的 web 门户。
- 系统通知走 `lib/desktop-notify.ts`，只发「需要人介入」与「终态」事件，且只在窗口失焦时发。

## 开发与打包

```bash
make desktop-deps          # 安装依赖（会下 ~100MB 的 Electron 二进制）
make desktop-dev           # 构建前端 + 起外壳（真实 app:// 路径）
cd src/desktop && npm run smoke   # 冒烟测试：真的加载一遍 SPA，退出码非 0 即失败
make desktop-dist          # 出当前平台安装包（未签名）
```

首次启动会要求填服务器地址（打 `GET /api/health` 校验）。内部分发可用环境变量
`POLARIS_DEFAULT_SERVER_URL` 预填，避免把内网地址写进源码。换服务器从菜单
「Server…」（Cmd+,）进入。

### 未签名分发的注意事项

- **macOS**：`identity: null` 只是「不用 Developer ID 签名」，它**不会**替你做 ad-hoc 签名。
  而 electron-builder 改包（换图标、塞 app.asar、放 extraResources）会让 Electron 预编译
  二进制自带的签名失效，Apple Silicon 内核**拒绝执行无有效签名的二进制**，用户会看到
  「已损坏」——这不是 quarantine 属性，`xattr` 删不掉。所以 `build/after-pack.cjs` 在打包后
  补一次 `codesign --force --deep --sign -` 并当场校验，签不上就让打包失败。
  这不能替代公证（notarization），只是让未签名分发能真的启动。
  分发用 zip 而不是 dmg（少一层隔离属性传播），用户首次打开仍需
  `xattr -dr com.apple.quarantine /Applications/Polaris.app` 或右键→打开。
- **Windows**：优先分发 zip 便携版，绕过 SmartScreen 对安装器的检查。
- **Linux**：AppImage 需宿主有 `libnss3 libgtk-3-0 libasound2`；Ubuntu 24.04+ 的 AppArmor
  限制或缺 SUID chrome-sandbox 时需要 `--no-sandbox` 启动——**写在说明里，不要在代码里
  默认关沙箱**。

## 后端侧

生产环境的 CORS 白名单必须包含 `app://polaris`（见 `src/backend/app/main.py`）。桌面端每个
请求都带 `Authorization` 头，必然触发预检，而 `allow_origins=[]` 时 Starlette 对预检直接
返回 400——这一点在客户端无法绕过（注入响应头改不了状态码）。

其余部署形态见 `docs/deployment.md`。
