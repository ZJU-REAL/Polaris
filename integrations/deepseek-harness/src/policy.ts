import type { Context } from '@deepseek-ai/cordis'
import type { Agent, PreStepDecision } from '@deepseek-ai/dsh-agent'
import type { UserMessage } from '@deepseek-ai/dsh-session'
import type { PostToolDecision, ToolExecutionResult } from '@deepseek-ai/dsh-tools'
import { isUserInvocable } from '@deepseek-ai/dsh-skill'
import type { PolarisSkillPolicy } from './contracts.js'
import type { PolarisSkillProvider } from './provider.js'

// Keep this token contract identical to @deepseek-ai/dsh-tool-skill.  Applying
// policy to a token that DSH did not recognize as an invocation would only
// make the turn more restrictive without loading the corresponding skill.
const SKILL_GESTURE = /(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)/g

interface ActiveRestriction {
  readonly turn: number
  allowed: Set<string>
  liftRestriction: (() => void) | undefined
  liftGuard: () => void
}

export function invokedSkillNames(messages: readonly UserMessage[]): string[] {
  const names: string[] = []
  for (const message of messages) {
    if ((message.source as { kind?: unknown }).kind !== 'user') continue
    for (const block of message.content) {
      if (block.type !== 'text') continue
      for (const match of block.text.matchAll(SKILL_GESTURE)) {
        const name = match[2]
        if (name !== undefined && !names.includes(name)) names.push(name)
      }
    }
  }
  return names
}

export function intersectAllowed(
  current: ReadonlySet<string> | undefined,
  incoming: readonly string[] | null,
): Set<string> | undefined {
  if (incoming === null) return current === undefined ? undefined : new Set(current)
  const next = new Set(incoming)
  if (current === undefined) return next
  return new Set([...current].filter(name => next.has(name)))
}

function currentTurn(agent: Agent): number | undefined {
  for (let index = agent.session.events.length - 1; index >= 0; index -= 1) {
    const event = agent.session.events[index]
    if (event?.type === 'turn/start') return event.data.turn
  }
  return undefined
}

function loadedPolarisSkill(result: Readonly<ToolExecutionResult>): string | undefined {
  if (result.isError || typeof result.value !== 'object' || result.value === null) return undefined
  const value = result.value as Record<string, unknown>
  return value.provider === 'polaris' && typeof value.name === 'string' ? value.name : undefined
}

export class PolarisSkillPolicyController {
  private readonly active = new Map<Agent, ActiveRestriction>()
  private rebuilding = false
  private readonly prefix: string

  constructor(
    private readonly ctx: Context,
    serverName: string,
    private readonly mode: 'enforce' | 'advisory' = 'enforce',
  ) {
    this.prefix = `mcp__${serverName}__`
  }

  apply(agent: Agent, turn: number, policy: PolarisSkillPolicy): void {
    let state = this.active.get(agent)
    if (state !== undefined && state.turn !== turn) {
      this.clear(agent)
      state = undefined
    }
    const allowed = intersectAllowed(state?.allowed, policy.allowedTools)
    if (allowed === undefined) return
    if (state === undefined) {
      const liftGuard = agent.ctx.tools.guard(exec => {
        const live = this.active.get(agent)
        if (live === undefined || !exec.name.startsWith(this.prefix)) return undefined
        const rawName = exec.name.slice(this.prefix.length)
        if (live.allowed.has(rawName)) return undefined
        const message = `Polaris skill policy denies tool "${rawName}" for turn ${live.turn}`
        if (this.mode === 'advisory') {
          this.ctx.logger.warn(`polaris-skill-policy: advisory: ${message}`)
          return undefined
        }
        return message
      })
      state = { turn, allowed, liftRestriction: undefined, liftGuard }
      this.active.set(agent, state)
    } else {
      state.allowed = allowed
    }
    this.rebuild(agent, state)
  }

  refreshAll(): void {
    if (this.rebuilding) return
    for (const [agent, state] of this.active) this.rebuild(agent, state)
  }

  clear(agent: Agent): void {
    const state = this.active.get(agent)
    if (state === undefined) return
    this.active.delete(agent)
    state.liftRestriction?.()
    state.liftGuard()
  }

  clearTurn(agent: Agent, turn: number): void {
    if (this.active.get(agent)?.turn === turn) this.clear(agent)
  }

  private rebuild(agent: Agent, state: ActiveRestriction): void {
    if (this.rebuilding) return
    this.rebuilding = true
    try {
      state.liftRestriction?.()
      state.liftRestriction = undefined
      if (this.mode === 'advisory') return
      const denied = agent.ctx.tools.schemas(agent)
        .map(tool => tool.name)
        .filter(name => name.startsWith(this.prefix))
        .filter(name => !state.allowed.has(name.slice(this.prefix.length)))
      if (denied.length > 0) {
        state.liftRestriction = agent.ctx.tools.restrict({ deny: denied })
      }
    } finally {
      this.rebuilding = false
    }
  }
}

export function installSkillPolicy(
  ctx: Context,
  provider: PolarisSkillProvider,
  serverName: string,
  mode: 'enforce' | 'advisory' = 'enforce',
): void {
  const controller = new PolarisSkillPolicyController(ctx, serverName, mode)

  ctx.on('tools/change', () => controller.refreshAll())
  ctx.on('agent/disposed', ({ agent }) => controller.clear(agent))
  ctx.on('agent/error', ({ agent, turn }) => controller.clearTurn(agent, turn))
  ctx.on('agent/turn-stopping', ({ agent, turn }) => controller.clearTurn(agent, turn))

  ctx.on('tools/post-execute', async (exec, result, next): Promise<PostToolDecision> => {
    const decision = await next()
    if (exec.name !== 'skill' || exec.agent === undefined) return decision
    const name = loadedPolarisSkill(result)
    const turn = currentTurn(exec.agent)
    const policy = name === undefined ? undefined : provider.policy(name)
    if (turn !== undefined && policy !== undefined) controller.apply(exec.agent, turn, policy)
    return decision
  })

  ctx.on('agent/pre-step', async (
    { agent, messages, turn, signal },
    next,
  ): Promise<PreStepDecision> => {
    const existing = currentTurn(agent)
    if (existing !== undefined && existing !== turn) controller.clear(agent)
    const decision = await next()
    if (decision.kind === 'reject') return decision
    const names = invokedSkillNames(messages)
    if (names.length === 0) return decision
    const summaries = await ctx.skills.list({
      cwd: agent.session.header.cwd,
      signal,
      scope: agent,
    })
    signal.throwIfAborted()
    for (const name of names) {
      const summary = summaries.find(skill => skill.name === name)
      if (summary?.provider !== 'polaris' || !isUserInvocable(summary)) continue
      const policy = provider.policy(name)
      if (policy !== undefined) controller.apply(agent, turn, policy)
    }
    return decision
  })
}
