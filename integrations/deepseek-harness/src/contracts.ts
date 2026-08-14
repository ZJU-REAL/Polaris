import { z } from 'zod'

export const SkillFileSchema = z.object({
  path: z.string().min(1),
  size: z.number().int().nonnegative(),
  revision: z.string().regex(/^[a-f0-9]{64}$/),
}).strict()

export const SkillCatalogItemSchema = z.object({
  id: z.string().uuid(),
  slug: z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/),
  name: z.string().min(1),
  description: z.string().min(1),
  invocation: z.enum(['auto', 'manual']),
  scope: z.enum(['builtin', 'user']),
  allowedTools: z.array(z.string().min(1)).nullable(),
  files: z.array(SkillFileSchema),
  revision: z.string().regex(/^[a-f0-9]{64}$/),
  updatedAt: z.iso.datetime({ offset: true }),
}).strict()

export const SkillCatalogSchema = z.object({
  revision: z.string().regex(/^[a-f0-9]{64}$/),
  skills: z.array(SkillCatalogItemSchema),
}).strict()

export const SkillDefinitionSchema = SkillCatalogItemSchema.extend({
  body: z.string(),
}).strict()

export type PolarisSkillCatalog = z.infer<typeof SkillCatalogSchema>
export type PolarisSkillCatalogItem = z.infer<typeof SkillCatalogItemSchema>
export type PolarisSkillDefinition = z.infer<typeof SkillDefinitionSchema>

export interface PolarisSkillPolicy {
  readonly provider: 'polaris'
  readonly name: string
  readonly allowedTools: readonly string[] | null
  readonly userInvocable: boolean
  readonly revision: string
}
