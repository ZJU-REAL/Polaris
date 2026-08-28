# Polaris Extension

This directory contains the least-privilege Manifest V3 reference client for the Polaris PDF download and archive protocol.

## Scope

- Connect to one user-configured Polaris instance with a user-scoped `pol_dl_` API key.
- Receive `polaris:download-batch:v2` events from that instance.
- Keep one local task per pushed batch and preserve an independent library/paper binding for every item.
- Fetch a selected PDF candidate or accept a user-selected local PDF.
- Verify the `%PDF-` signature, size, and SHA-256 before caching bytes in Cache Storage.
- Archive cached files to `/api/download-client/archive` with each paper's own identity metadata.

The core client does not include YFR page integration, SCNet automation, Zotero integration, native messaging, publisher-specific automation, CAPTCHA handling, or a fixed-directory bridge.

## Install for development

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this directory.
4. Open the side panel, enter the Polaris origin and a `pol_dl_` API key, then test and save the connection.

The extension requests access to the configured Polaris origin when the user saves the connection. Candidate PDF origins are requested only when the user starts a download. It has no install-time host access.

## Protocol

Polaris dispatches a `polaris:download-batch:v2` DOM event. The dynamically registered bridge forwards the payload to the service worker and returns a `polaris:download-batch-ack:v2` event. A batch becomes one task containing multiple items; an item is always archived by its own `library_id`, `paper_id`, nonce, and bibliographic identity.

## Tests

```bash
npm run check
```

The tests cover origin and permission normalization, expiring batch validation, independent per-paper bindings, PDF verification and hashing, archive metadata, and manifest permission boundaries.
