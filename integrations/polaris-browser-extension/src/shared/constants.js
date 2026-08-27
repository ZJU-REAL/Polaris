export const SCHEMA_VERSION = 1;
export const DB_NAME = "yfr-pdf-downloader";
export const DB_VERSION = 1;
export const MAX_IMPORT_ITEMS = 1000;
export const DEFAULT_MAX_PDF_BYTES = 150 * 1024 * 1024;
export const DEFAULT_GLOBAL_CONCURRENCY = 2;
export const DEFAULT_PUBLISHER_CONCURRENCY = 1;
export const NATIVE_HOST_NAME = "com.yfr.download_bridge";
export const MINIMUM_NATIVE_BRIDGE_VERSION = "0.4.1";
export const YFR_PUBLIC_SEARCH_URL = "https://yfr.yangy.cn/literature-search/?yfr-download=1";
export const BRIDGE_INSTALLER_URL = "https://github.com/yyy-OPS/YFR.frontier-review-daily/releases/latest/download/YFRDownloadBridgeSetup.exe";
export const BUNDLED_BRIDGE_INSTALLER_PATH = "assets/YFRDownloadBridgeSetup.exe";

export const MESSAGE = Object.freeze({
  YFR_IMPORT_SELECTION: "YFR_IMPORT_SELECTION",
  POLARIS_IMPORT_TASK: "POLARIS_IMPORT_TASK",
  POLARIS_IMPORT_BATCH: "POLARIS_IMPORT_BATCH",
  TEST_POLARIS_CONNECTION: "TEST_POLARIS_CONNECTION",
  SAVE_POLARIS_CONNECTION: "SAVE_POLARIS_CONNECTION",
  AUTHORIZE_POLARIS_CONNECTION: "AUTHORIZE_POLARIS_CONNECTION",
  TOGGLE_YFR_PAGE_SELECTION: "TOGGLE_YFR_PAGE_SELECTION",
  IMPORT_RECORDS: "IMPORT_RECORDS",
  GET_STATE: "GET_STATE",
  START_REGISTRATION: "START_REGISTRATION",
  START_CACHING: "START_CACHING",
  START_DOWNLOADS: "START_DOWNLOADS",
  STOP_TASK: "STOP_TASK",
  RESUME_TASK: "RESUME_TASK",
  RETRY_ITEM: "RETRY_ITEM",
  REPARSE_ITEM: "REPARSE_ITEM",
  ABANDON_ITEM: "ABANDON_ITEM",
  RECHECK_PUBLISHER: "RECHECK_PUBLISHER",
  OPEN_MANUAL_PAGE: "OPEN_MANUAL_PAGE",
  OPEN_CACHED_PDF: "OPEN_CACHED_PDF",
  START_ASSISTED_PDF_CAPTURE: "START_ASSISTED_PDF_CAPTURE",
  STOP_ASSISTED_PDF_CAPTURE: "STOP_ASSISTED_PDF_CAPTURE",
  APPROVE_CACHED_PDF: "APPROVE_CACHED_PDF",
  CHOOSE_DESTINATION: "CHOOSE_DESTINATION",
  UPDATE_SETTINGS: "UPDATE_SETTINGS",
  DOWNLOAD_BRIDGE_INSTALLER: "DOWNLOAD_BRIDGE_INSTALLER",
  OPEN_BRIDGE_INSTALLER: "OPEN_BRIDGE_INSTALLER",
  SHOW_BRIDGE_INSTALLER: "SHOW_BRIDGE_INSTALLER",
  REFRESH_BRIDGE_STATUS: "REFRESH_BRIDGE_STATUS",
  REFRESH_ZOTERO_STATUS: "REFRESH_ZOTERO_STATUS",
  PAIR_ZOTERO: "PAIR_ZOTERO",
  DISCONNECT_ZOTERO: "DISCONNECT_ZOTERO",
  UPDATE_ZOTERO_SETTINGS: "UPDATE_ZOTERO_SETTINGS",
  RETRY_ZOTERO_ITEM: "RETRY_ZOTERO_ITEM",
  RETRY_ZOTERO_PENDING: "RETRY_ZOTERO_PENDING",
  STATE_CHANGED: "STATE_CHANGED",
  PROBE_PUBLISHER_PAGE: "PROBE_PUBLISHER_PAGE",
  PREFLIGHT_PDF_CANDIDATE: "PREFLIGHT_PDF_CANDIDATE",
  NAVIGATE_PUBLISHER_PDF: "NAVIGATE_PUBLISHER_PDF",
  SCNET_SYNC_SNAPSHOT: "SCNET_SYNC_SNAPSHOT",
  SCNET_REFRESH: "SCNET_REFRESH",
  SCNET_SAVE_CONTEXT: "SCNET_SAVE_CONTEXT",
  SCNET_GUI_OPEN: "SCNET_GUI_OPEN",
  SCNET_GUI_STATUS: "SCNET_GUI_STATUS",
  SCNET_GUI_COLLECT: "SCNET_GUI_COLLECT",
});

export const ITEM_STATE = Object.freeze({
  PENDING: "pending",
  RESOLVING: "resolving",
  CANDIDATE_REGISTERED: "candidate_registered",
  PDF_RESPONSE_VERIFIED: "pdf_response_verified",
  CACHING: "caching",
  PDF_CACHED: "pdf_cached",
  ARCHIVING: "archiving",
  AUTHORIZED: "authorized",
  LOGIN_REQUIRED: "login_required",
  MANUAL_REQUIRED: "manual_required",
  NO_ENTITLEMENT: "no_entitlement",
  BLOCKED: "blocked",
  QUEUED: "queued",
  DOWNLOADING: "downloading",
  VERIFYING: "verifying",
  COMPLETED: "completed",
  BROWSER_DOWNLOADED: "browser_downloaded",
  VERIFICATION_INCONCLUSIVE: "verification_inconclusive",
  INVALID_RESPONSE: "invalid_response",
  QUARANTINED: "quarantined",
  ABANDONED: "abandoned",
  FAILED: "failed",
});

export const KNOWN_PUBLISHERS = Object.freeze([
  { id: "sciencedirect", label: "ScienceDirect / Elsevier", hosts: ["sciencedirect.com", "linkinghub.elsevier.com"] },
  { id: "springer", label: "SpringerLink / SpringerOpen / Nature", hosts: ["link.springer.com", "springeropen.com", "nature.com"] },
  { id: "wiley", label: "Wiley Online Library", hosts: ["onlinelibrary.wiley.com"] },
  { id: "taylor-francis", label: "Taylor & Francis", hosts: ["tandfonline.com"] },
  { id: "sage", label: "SAGE Journals", hosts: ["journals.sagepub.com"] },
  { id: "ieee", label: "IEEE Xplore", hosts: ["ieeexplore.ieee.org"] },
  { id: "iop", label: "IOPscience", hosts: ["iopscience.iop.org"] },
  { id: "aps", label: "APS Journals", hosts: ["journals.aps.org"] },
  { id: "science", label: "Science / AAAS", hosts: ["science.org"] },
  { id: "quantum", label: "Quantum", hosts: ["quantum-journal.org"] },
]);
