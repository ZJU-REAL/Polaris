import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-skill'
import { PolarisClient } from './client.js'
import { installSkillPolicy } from './policy.js'
import { PolarisSkillProvider } from './provider.js'

export const name = 'polaris-deepseek-harness'
export const inject = ['agents', 'tools', 'skills']

export interface Config {
  readonly baseUrl: string
  readonly token: string
  readonly serverName?: string
  readonly refreshIntervalMs?: number
  readonly requestTimeoutMs?: number
  readonly failOnStartupError?: boolean
  readonly allowedToolsMode?: 'enforce' | 'advisory' | 'off'
  readonly userSkillRank?: number
  readonly builtinSkillRank?: number
}

const SERVER_NAME = /^[A-Za-z0-9_-]{1,32}$/

export const Config: z<Config> = z.object({
  baseUrl: z.string().required(),
  token: z.string().required().role('secret'),
  serverName: z.string().pattern(SERVER_NAME).default('polaris'),
  refreshIntervalMs: z.number().min(1000).default(30_000),
  requestTimeoutMs: z.number().min(1).default(10_000),
  failOnStartupError: z.boolean().default(false),
  allowedToolsMode: z.union(['enforce', 'advisory', 'off']).default('enforce'),
  userSkillRank: z.number().default(340),
  builtinSkillRank: z.number().default(360),
})

export async function apply(ctx: Context, config: Config): Promise<void> {
  const serverName = config.serverName ?? 'polaris'
  const refreshIntervalMs = config.refreshIntervalMs ?? 30_000
  const requestTimeoutMs = config.requestTimeoutMs ?? 10_000
  const client = new PolarisClient(config.baseUrl, config.token, requestTimeoutMs)
  const provider = new PolarisSkillProvider(
    ctx,
    client,
    {
      user: config.userSkillRank ?? 340,
      builtin: config.builtinSkillRank ?? 360,
    },
    refreshIntervalMs,
  )

  if (config.failOnStartupError === true) await provider.initialize()
  ctx.skills.registerProvider(control => {
    provider.bind(control)
    return provider
  })

  const resourceTool = defineTool({
    name: 'polaris_skill_resource',
    description: 'Read one text attachment named by a loaded Polaris skill. Use only paths listed in that skill.',
    parameters: {
      skill: { type: 'string', required: true, description: 'Exact Polaris skill name.' },
      path: { type: 'string', required: true, description: 'Relative attachment path listed by the skill.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          skill: { type: 'string', required: true },
          path: { type: 'string', required: true },
          content: { type: 'string', required: true },
          revision: { type: 'string' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.content }],
    },
    async execute(args, exec) {
      const result = await client.fetchSkillFile(args.skill, args.path, exec.signal)
      if (result === undefined) {
        throw new Error(`Polaris skill "${args.skill}" has no attachment "${args.path}"`)
      }
      return {
        skill: args.skill,
        path: args.path,
        content: result.content,
        ...(result.revision === undefined ? {} : { revision: result.revision }),
      }
    },
    isConcurrencySafe: () => true,
    presentCall: args => ({
      card: 'generic',
      title: `Read Polaris skill resource ${args.skill}/${args.path}`,
      kind: 'read',
      rawInput: `${args.skill}/${args.path}`,
    }),
  })
  ctx.tools.register(resourceTool)

  const policyMode = config.allowedToolsMode ?? 'enforce'
  if (policyMode !== 'off') {
    installSkillPolicy(ctx, provider, serverName, policyMode)
  }
}

export { PolarisApiError, PolarisClient } from './client.js'
export { intersectAllowed, invokedSkillNames, PolarisSkillPolicyController } from './policy.js'
export { PolarisSkillProvider } from './provider.js'
export type {
  PolarisSkillCatalog,
  PolarisSkillCatalogItem,
  PolarisSkillDefinition,
  PolarisSkillPolicy,
} from './contracts.js'
