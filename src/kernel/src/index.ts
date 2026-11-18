export { Kernel, createKernel, type KernelOptions } from './kernel.ts'
export {
  CONFIG_TREE_BACKUP_PREFIX,
  createPluginHost,
  dropUnknownBuiltins,
  marketPluginsDir,
  mountEngine,
  type CreatePluginHostOptions,
  type PluginHost,
  type PluginHostLog,
  type TreeRepair,
} from './host.ts'
export {
  JsonRpcEndpoint,
  type JsonRpcEndpointOptions,
  type NotificationHandler,
  type RpcHandler,
} from './rpc/jsonrpc.ts'
export { JobBus, type JobEvent } from './rpc/jobs.ts'
export {
  MARKET_ENDPOINT_META_KEY,
  createMarketMethods,
  type MarketEndpointInfo,
  type MarketMethods,
  type MarketRpcDeps,
} from './rpc/market-methods.ts'
export {
  ERR_CAPABILITY_UNAVAILABLE,
  ERR_INVALID_PARAMS,
  MAX_TREE_DEPTH,
  asNumber,
  asPluginConfig,
  asPluginTreeExport,
  asString,
  createPluginMethods,
  type PluginRpcDeps,
  type PluginTreeExportShape,
  type RpcMethod,
} from './rpc/plugin-methods.ts'
export {
  MemoryConfigTreeStore,
  type ConfigEntry,
  type ConfigTreeStore,
} from './config/tree.ts'
export {
  CONFIG_TREE_LAST_GOOD_KEY,
  SqliteTree,
  describeEntryState,
  type LastGoodSink,
  type PluginEntryInfo,
  type PluginValidationError,
  type PluginValidationResult,
} from './config/sqlite-tree.ts'
export { BUILTIN_PLUGINS, registerBuiltins } from './plugins/builtins.ts'
export {
  ENGINE_CONTAINER,
  ENGINE_SECRET_ENV,
  EnginePortError,
  LegacyEngineConfig,
  buildEngineArgv,
  enginePortProblem,
  generateFernetKey,
  legacyEngine,
  loadOrCreateEngineSecrets,
  portInUse,
  type EnginePortProblem,
  type EngineSecrets,
  type EngineState,
  type EngineStateEvent,
  type LegacyEngineService,
} from './plugins/legacy-engine.ts'
export {
  RotatingLog,
  createSecretScrubber,
  scrubProcessSecrets,
  tailLogFile,
  type RotatingLogOptions,
  type Scrubber,
} from './log/rotating-log.ts'
export {
  ENGINE_LLM_ENV,
  SHELL_ENV_TIMEOUT_MS,
  SHELL_NETWORK_ENV,
  applyShellEnv,
  ensureShellEnv,
  parseShellEnv,
  resolveShellEnv,
  runShell,
  type ShellEnvMarkers,
  type ShellEnvOptions,
  type ShellRunOptions,
  type ShellRunner,
} from './shell-env.ts'
export {
  MIGRATIONS,
  SNAPSHOT_KEEP,
  migrate,
  openStorage,
  openStorageWithMigrations,
  pruneSnapshots,
  restoreSnapshot,
  snapshotDatabase,
  type Migration,
} from './storage/db.ts'
export { PluginMetaStore, SqliteConfigTreeStore } from './storage/store.ts'
export { StorageConfig, storage, type StorageService } from './plugins/storage.ts'
export {
  MarketError,
  PLUGIN_KINDS,
  PLUGIN_RUNTIMES,
  QUALITY_TIERS,
  isValidPackageName,
  lintDescription,
  validateMarketIndex,
  validateMarketIndexEntry,
  validatePolarisManifest,
  type FetchImpl,
  type InstallRecord,
  type ManifestPermissions,
  type MarketErrorCode,
  type MarketIndex,
  type MarketIndexEntry,
  type MarketPermissions,
  type PluginKind,
  type PluginRuntime,
  type PolarisManifest,
  type QualityTier,
} from './market/contract.ts'
export { OFFICIAL_INDEX_URL, fetchIndex, type FetchIndexOptions } from './market/index-client.ts'
export {
  NPM_REGISTRY,
  installPlugin,
  parseTar,
  uninstallPlugin,
  verifyEntryHash,
  type InstallPhase,
  type InstallPluginOptions,
  type UninstallPluginOptions,
} from './market/install.ts'
export {
  INSTALL_RECORD_PREFIX,
  createImportGuard,
  entryFileUrl,
  installAndRegister,
  installRecordKey,
  packageEntryId,
  uninstallAndRemove,
  verifyInstalledEntries,
  type ImportGuardOptions,
  type InstallAndRegisterOptions,
  type InstallAndRegisterResult,
  type InstallVerifyIssue,
  type PluginMetaLike,
  type UninstallAndRemoveOptions,
  type UninstallOutcome,
  type VerifyInstalledOptions,
} from './market/lifecycle.ts'
export { Context } from '@deepseek-ai/cordis'
// 与上一行的 Context 同理：桌面侧建树要 ctx.plugin(Loader)，从 kernel
// 统一转出，避免 desktop 直接依赖 vendor 包名
export { Loader } from '@deepseek-ai/cordis-plugin-loader'
