# Polaris Browser Extension

Polaris Browser Extension is a Chrome/Edge Manifest V3 extension bundled with Polaris. It receives papers already admitted to a Polaris library, caches authorized PDFs, archives them to the exact Polaris paper, and reads the current SCNet template state through the browser's authenticated session.

Current version: `0.7.4`. The extension does not require a separately deployed YFR service. Legacy YFR page selection remains available only as an optional compatibility input.

## Supported environment

- Chrome 116+ or Microsoft Edge 116+
- A Polaris instance and a user-generated `pol_dl_` API Key
- Optional Windows 10/11 x64 local bridge
- Institution-authorized, campus-network, VPN, CARSI, WebVPN/EZproxy, or open-access sessions already active in the current browser

The extension does not bypass CAPTCHA, paywalls, DRM, publisher authorization, or institutional access controls.

## SCNet 模板桥接

这是 Polaris 的统一浏览器扩展，PDF 任务和 SCNet 模板桥接共用同一个 Service Worker。打开已登录的 `www.scnet.cn` 模板中心后，扩展会使用当前页面的同源会话读取模板目录、订阅状态、版本和非计费可用性证据，并只把脱敏 JSON 同步到 Polaris。原始 Cookie、Authorization、请求头、AK/SK、令牌和用户目录不会离开浏览器。

在 Polaris 页面完成一次 SCNet 凭据上下文绑定后，扩展会把模板快照写入该 `ScnetCredential`。多个 SCNet 账号必须分别绑定，快照、区域、队列、预算和作业上下文不会混用。用户在 SCNet 官方页面完成订阅或开通后，触发扩展的重新校验事件即可刷新状态；扩展不会自动购买、开通或充值。

模板状态分为“目录存在、已订阅、已授权、GUI/模板/CMD 可用、区域资源可用、实际运行已验证”。模板中心显示或商品启用不等于求解成功。Polaris 的真实上传、提交、轮询、结果下载和解析继续走后端 SCNet OpenAPI；收费求解默认需要用户在提交门禁中确认，并展示账号、区域、队列、预算、输入哈希和模型/网格预览。

## Zotero local sync

Version 0.5.2 can discover YFR Zotero Companion on `127.0.0.1:23119-23123`, pair through a six-digit one-time code, and automatically copy each bridge-archived PDF into Zotero storage under its exact parent item. Zotero creates the parent item and `YFR / topic / date` collection when needed while retaining the bridge source file. DOI conflicts, file hash mismatches, ambiguous title fallbacks, and network paths are rejected rather than attached to the wrong paper.

PDF archive state is independent from Zotero availability. When Zotero is offline, completed files remain archived and enter a pending sync state; the extension retries after Zotero becomes available. Pairing tokens and full local paths are never written to extension activity logs.

## Load the extension

1. Open `chrome://extensions/` or `edge://extensions/`.
2. Enable developer mode.
3. Choose **Load unpacked** and select this `src/browser-extension` directory.
4. In Polaris, generate a download-client API Key and configure the instance address and key in the extension.
5. Push an admitted paper from its Polaris library to the extension, cache the authorized PDF, then choose Archive.

Clicking the extension icon opens the Polaris extension side panel. On a legacy YFR literature page only, the icon keeps the compatibility selection behavior.

## Page selection

Selection mode supports:

- select all;
- invert selection;
- select entries with a visible PDF candidate;
- clear selection;
- push selected records to the extension;
- dynamic React list updates without duplicate tasks.

YFR server paper IDs, source area, search/review run ID, ordered paper-ID list, and selected count are validated as one import contract. The extension restores exactly the records selected on the source page and rejects count, order, or duplicate-ID mismatches instead of silently reducing a task.

Only visible scholarly metadata is extracted: title, authors, year, venue, DOI, article URL, and PDF candidate URL. Sensitive URL parameters are removed before persistence.

## Optional local bridge

The offline extension bundle includes `YFRDownloadBridgeSetup.exe` and uses that local asset first. The slim bundle falls back to the fixed asset name on the project's GitHub Releases page. The user must explicitly open the installer and confirm Windows prompts.

The bridge:

- installs under `%LOCALAPPDATA%/YFRDownloadBridge/app`;
- registers only under the current user for Chrome and Edge;
- requires no administrator privilege and no separate .NET runtime;
- accepts connections only from extension ID `ikinkjjfnpikbjlekpbdojnflldbnkjg`;
- contains no network client;
- validates PDF signature, size, SHA-256, DOI, and title before final placement.

For Polaris tasks, version 0.6.3 uses a strict local-first archive order. After browser-cache verification, the extension asks the bridge to write the PDF into the configured fixed directory and persists the returned local file record. Only then does it upload the same verified PDF to the exact Polaris `libraryId` and `paperId`. If cloud synchronization fails, the local file and browser cache remain available; retrying performs only the Polaris synchronization and does not create another local copy.

Version 0.6.4 imports one Polaris push as one download task containing all selected papers. A task processes up to two papers concurrently while limiting a known publisher to one active paper. Each item retains its own `libraryId`, `paperId`, `searchHitId`, and nonce; task IDs, ordinals, and array positions are never used to locate the cloud archive target. Local bridge finalization is serialized to protect the on-disk manifest, while browser caching and Polaris uploads can continue concurrently.

Version 0.6.6 recognizes WCEE `downloadFile` attachment routes even though their URLs and button labels do not contain `PDF`. JavaScript-driven publisher buttons are captured from the resulting `Document`, `XHR`, `Fetch`, or browser download response, including `application/octet-stream` attachments. For an unsupported publisher, each paper exposes **Capture PDF**: the user starts capture on that exact paper, clicks the publisher's PDF or Download button in the current tab within 120 seconds, and the extension binds the verified bytes only to that paper. Chrome download events are treated as candidate evidence only; a `.pdf` filename, MIME type, or download popup never bypasses the final `%PDF-` signature and size checks. Capture listeners are isolated to the current tab and removed on success, stop, timeout, or extension restart.

The browser remains the authenticated transport. It streams each selected PDF into extension-owned Cache Storage using the active campus-network, CARSI, VPN, WebVPN, publisher-cookie, and human-verification session. The extension then sends verified PDF bytes to the bridge in bounded chunks; browser cookies, credentials, and signed publisher URLs are never exported. The bridge removes non-PDF responses, places identity-inconclusive PDFs under `needs-review`, quarantines mismatched papers, and moves verified files into the selected destination.

For ScienceDirect, the extension first tries the current authenticated browser session directly: it follows the exact `/pii/<PII>/pdfft` View PDF entry, follows both same-tab and new-tab navigation, binds the final signed asset by PII, validates `%PDF-`, and caches it before advancing. If the publisher returns a CAPTCHA, login page, forbidden response, or an incomplete navigation, the queue pauses on that paper and asks the user to complete the challenge before retrying. It never solves or bypasses the challenge.

Selected papers retain their original YFR paper IDs, ordinals, and filenames and are processed strictly in that order. The queue advances only after the current paper has produced a `%PDF-`-verified Cache Storage entry or the user explicitly abandons that paper. Login, CAPTCHA, and other manual states pause the queue on the current paper; stopping preserves that gate and the remaining queue, and **Resume** continues from the same paper.

Cache Storage is disk-backed rather than an in-memory PDF buffer. The extension displays Chromium's current usage and quota estimate, preserves a free-space reserve, and enforces a configurable per-paper limit of at most 150 MiB. Naming templates are applied to pending items and always retain a stable paper index.

Extension version 0.5.2 uses two verified transport paths. Public endpoints such as Quantum are streamed directly into Cache Storage and checked for the `%PDF-` signature. Session-bound publishers use the exact PDF link or page control discovered on the article page, then capture the current tab or child tab's trusted navigation and redirect chain. Cross-origin redirects and Chromium PDF Viewer `206 Partial Content` ranges are accepted only when bound to that chain; unrelated page resources, advertising iframes, supplementary files, posters, HTML challenges, and incomplete responses are rejected. Publisher responses marked `Content-Disposition: attachment` are replayed as inline PDF responses after signature verification, so Wiley-style endpoints do not enter Chromium's download manager. Wiley first opens the matching ePDF reader in the existing publisher tab, waits until that exact DOI page is available, and then triggers `pdfdirect` in the same tab. IEEE follows the same session-safe pattern from `stamp.jsp` to `stampPDF/getPDF.jsp`, with the requested `arnumber` enforced during capture. Signed Silverchair assets, APS Accepted manuscripts, Optica `viewmedia` routes, and Optics Journal `GetArticlePDF` readers use the same attach-before-navigation path. A reader that is already open is reloaded only after capture attaches. Optica and Optics Journal readers remain attached through a temporary `200` access-check page and accept a later PDF response with `application/pdf`, `application/octet-stream`, or a PDF content-disposition only after its `%PDF-` signature verifies. An internal PDF response does not need a `.pdf` URL suffix. Temporary credentials remain in the active browser session only; persisted candidates and logs are redacted and retries rediscover a fresh URL from the DOI or article page. These session-only routes are never retried through an extension-background fetch. Current explicit rules cover Quantum, Wiley, IOPscience, APS, IEEE Xplore, ACS, Science/AAAS, Optica, Optics Journal, arXiv, and Frontiers, while the page scanner handles other HTTP/HTTPS publishers without a domain allowlist.

ScienceDirect keeps its stricter PII-bound flow. The extension captures the browser's own authorized PDF navigation response before it enters Chromium's PDF Viewer, validates `%PDF-`, stores those same bytes in extension Cache Storage, and returns the same PDF body as an inline response to the Viewer. It does not issue a second request for the signed asset. The temporary browser debugging attachment is limited to active paper tabs and is removed immediately after capture or failure.

Any paper state can be reset with **Reparse from DOI**. This discards stale candidates and temporary cache state, starts again from the DOI resolver when available, and keeps prior archive metadata for audit without deleting files already written to disk. Bridge version 0.4.1 or later rebuilds bounded text from PDF words, verifies exact DOI boundaries first, and falls back to title only when the inspected PDF pages expose no DOI. Ambiguous identities stay in browser cache for review in the extension's read-only viewer; explicit user approval is recorded as `manual-confirmed`, not strict automatic verification. Active preflight, capture, cache, and archive queues can be stopped from the side panel.

Publisher discovery and PDF preflight support arbitrary HTTP and HTTPS publisher hosts without a publisher-domain allowlist. This removes CORS failures caused by incomplete publisher-domain coverage, including redirect and tracking hosts. The extension still rejects non-web protocols and does not request the `cookies` permission.

## Verification

```powershell
cd apps/browser-extension
npm test
npm run audit
npm run smoke
npm run smoke:browser
npm run build
```

Build the Windows x64 setup executable:

```powershell
powershell -ExecutionPolicy Bypass -File tools/yfr-download-bridge/scripts/build-release.ps1
```

The release asset must be named `YFRDownloadBridgeSetup.exe`.
