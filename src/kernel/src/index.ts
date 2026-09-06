export { Kernel, createKernel, type KernelOptions } from './kernel.ts'
export {
  JsonRpcEndpoint,
  type JsonRpcEndpointOptions,
  type NotificationHandler,
  type RpcHandler,
} from './rpc/jsonrpc.ts'
export {
  MemoryConfigTreeStore,
  type ConfigEntry,
  type ConfigTreeStore,
} from './config/tree.ts'
export {
  ENGINE_CONTAINER,
  LegacyEngineConfig,
  buildEngineArgv,
  legacyEngine,
  type LegacyEngineService,
} from './plugins/legacy-engine.ts'
export { MIGRATIONS, migrate, openStorage, type Migration } from './storage/db.ts'
export { PluginMetaStore, SqliteConfigTreeStore } from './storage/store.ts'
export { StorageConfig, storage, type StorageService } from './plugins/storage.ts'
export {
  type FetchImpl,
  type SourceAdapter,
  type SourceAuthor,
  type SourceRecord,
  type SourceSearchOptions,
} from './sources/contract.ts'
export { OPENALEX_BASE, OpenAlexAdapter, openAlexRecord, type OpenAlexAdapterOptions } from './sources/openalex.ts'
export {
  CORDIS_BASE,
  CordisProjectsAdapter,
  buildCordisQuery,
  cordisDetailRecord,
  cordisSearchRecord,
  type CordisProjectsAdapterOptions,
} from './sources/cordis-projects.ts'
export { SourcesConfig, sources, type SourcesService } from './plugins/sources.ts'
export { Context } from '@deepseek-ai/cordis'
