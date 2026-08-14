import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { UserMessage } from '@deepseek-ai/dsh-session'
import { describe, expect, it, vi } from 'vitest'
import {
  intersectAllowed,
  invokedSkillNames,
  PolarisSkillPolicyController,
} from '../src/policy.js'

describe('skill policy helpers', () => {
  it('extracts unique explicit skill gestures only from user text', () => {
    const messages = [{
      source: { kind: 'user' },
      content: [
        { type: 'text', text: 'Please /paper-triage and /paper-triage now.' },
        { type: 'text', text: 'Then /write-review' },
      ],
    }, {
      source: { kind: 'assistant' },
      content: [{ type: 'text', text: '/ignored-skill' }],
    }] as unknown as readonly UserMessage[]

    expect(invokedSkillNames(messages)).toEqual(['paper-triage', 'write-review'])
  })

  it('intersects multiple skill allowlists without ever widening', () => {
    expect(intersectAllowed(undefined, null)).toBeUndefined()
    expect([...intersectAllowed(undefined, ['search_papers', 'get_paper'])!]).toEqual([
      'search_papers',
      'get_paper',
    ])
    expect([
      ...intersectAllowed(new Set(['search_papers', 'get_paper']), ['get_paper', 'remember'])!,
    ]).toEqual(['get_paper'])
  })

  it('hides and guards denied Polaris MCP tools for the active turn', () => {
    let guard: ((exec: { name: string }) => string | undefined) | undefined
    let denied: readonly string[] = []
    const liftGuard = vi.fn()
    const liftRestriction = vi.fn()
    const tools = {
      guard: vi.fn((callback: typeof guard) => {
        guard = callback
        return liftGuard
      }),
      schemas: vi.fn(() => [
        { name: 'mcp__polaris__search_papers' },
        { name: 'mcp__polaris__get_paper' },
        { name: 'skill' },
      ]),
      restrict: vi.fn(({ deny }: { deny: readonly string[] }) => {
        denied = deny
        return liftRestriction
      }),
    }
    const agent = {
      ctx: { tools },
      session: { events: [{ type: 'turn/start', data: { turn: 7 } }] },
    } as unknown as Agent
    const controller = new PolarisSkillPolicyController({} as Context, 'polaris')

    controller.apply(agent, 7, {
      provider: 'polaris',
      name: 'paper-triage',
      allowedTools: ['search_papers'],
      userInvocable: true,
      revision: 'c'.repeat(64),
    })
    expect(denied).toEqual(['mcp__polaris__get_paper'])
    expect(guard?.({ name: 'mcp__polaris__search_papers' })).toBeUndefined()
    expect(guard?.({ name: 'mcp__polaris__get_paper' })).toContain('denies tool')
    expect(guard?.({ name: 'skill' })).toBeUndefined()

    controller.clearTurn(agent, 7)
    expect(liftRestriction).toHaveBeenCalledOnce()
    expect(liftGuard).toHaveBeenCalledOnce()
  })

  it('reports advisory violations without hiding or blocking tools', () => {
    let guard: ((exec: { name: string }) => string | undefined) | undefined
    const warn = vi.fn()
    const tools = {
      guard: vi.fn((callback: typeof guard) => {
        guard = callback
        return vi.fn()
      }),
      schemas: vi.fn(() => [{ name: 'mcp__polaris__get_paper' }]),
      restrict: vi.fn(),
    }
    const agent = {
      ctx: { tools },
      session: { events: [{ type: 'turn/start', data: { turn: 8 } }] },
    } as unknown as Agent
    const controller = new PolarisSkillPolicyController(
      { logger: { warn } } as unknown as Context,
      'polaris',
      'advisory',
    )

    controller.apply(agent, 8, {
      provider: 'polaris',
      name: 'paper-triage',
      allowedTools: ['search_papers'],
      userInvocable: true,
      revision: 'd'.repeat(64),
    })

    expect(tools.restrict).not.toHaveBeenCalled()
    expect(guard?.({ name: 'mcp__polaris__get_paper' })).toBeUndefined()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('advisory'))
  })
})
