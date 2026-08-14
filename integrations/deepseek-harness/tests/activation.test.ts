import { Context } from '@deepseek-ai/cordis'
import AgentRegistry from '@deepseek-ai/dsh-agent'
import SkillRegistry from '@deepseek-ai/dsh-skill'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { describe, expect, it } from 'vitest'
import * as PolarisPlugin from '../src/index.js'

describe('DSH plugin activation', () => {
  it('registers and disposes against the real Cordis service stack', async () => {
    const ctx = new Context()
    await ctx.plugin(SystemPrompt)
    await ctx.plugin(ToolRuntime)
    await ctx.plugin(AgentRegistry)
    await ctx.plugin(SkillRegistry)

    const fiber = await ctx.plugin(PolarisPlugin, {
      baseUrl: 'http://127.0.0.1:9',
      token: 'test-token',
      failOnStartupError: false,
    })
    expect(ctx.tools.schemas().map(tool => tool.name)).toContain(
      'polaris_skill_resource',
    )

    await fiber.dispose()
    expect(ctx.tools.schemas().map(tool => tool.name)).not.toContain(
      'polaris_skill_resource',
    )
  })
})
