/* The demo chain: register → project → add paper (bibtex, offline) →
   wiki compile (fake librarian) → full-text index rebuild (ARQ worker).
   Everything deterministic and key-free. */

interface Ctx {
  baseUrl: string
  token?: string
}

async function api(ctx: Ctx, method: string, path: string, body?: unknown, form = false): Promise<any> {
  const headers: Record<string, string> = {}
  if (ctx.token) headers.Authorization = `Bearer ${ctx.token}`
  let payload: BodyInit | undefined
  if (body !== undefined) {
    if (form) {
      headers['Content-Type'] = 'application/x-www-form-urlencoded'
      payload = new URLSearchParams(body as Record<string, string>).toString()
    } else {
      headers['Content-Type'] = 'application/json'
      payload = JSON.stringify(body)
    }
  }
  const res = await fetch(`${ctx.baseUrl}${path}`, { method, headers, body: payload })
  const text = await res.text()
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}: ${text.slice(0, 300)}`)
  return text ? JSON.parse(text) : null
}

const BIBTEX = `@article{spike2026polaris,
  title = {Deterministic Legacy Chain Probe for the Polaris Kernel Spike},
  author = {Probe, Spike and Kernel, Polaris},
  journal = {Journal of Reproducible Plumbing},
  year = {2026},
  abstract = {This synthetic record exercises the manual-import path, the fake
librarian compile, and the full-text index rebuild without any network access.}
}`

export interface ChainResult {
  paperId: string
  token: string
  wikiMarkdown: string
  indexStatus: string
}

/** Re-run the wiki compile for an existing paper (determinism probe). */
export async function recompilePaper(baseUrl: string, token: string, paperId: string): Promise<string> {
  const ctx: Ctx = { baseUrl, token }
  const compiled = await api(ctx, 'POST', `/api/papers/${paperId}/recompile`)
  return compiled.wiki_content ?? ''
}

export async function runChain(baseUrl: string): Promise<ChainResult> {
  const ctx: Ctx = { baseUrl }

  // First registered user becomes admin (legacy bootstrap); static invite code.
  const email = 'spike@example.com'
  const password = 'spike-passw0rd!'
  try {
    await api(ctx, 'POST', '/api/auth/register', {
      email,
      password,
      display_name: 'Spike Probe',
      username: 'spikeprobe',
      invite_code: 'polaris-lab',
    })
  } catch (err) {
    if (!/REGISTER_USER_ALREADY_EXISTS|USERNAME_TAKEN/.test(String(err))) throw err
  }
  const login = await api(ctx, 'POST', '/api/auth/jwt/login', { username: email, password }, true)
  ctx.token = login.access_token

  // Manual import writes into a library the user owns, linked via the project.
  const library = await api(ctx, 'POST', '/api/libraries', {
    name: 'Spike Library',
    statement:
      'Deterministic probe library for the kernel spike chain, covering reproducible plumbing research.',
  })
  const project = await api(ctx, 'POST', '/api/projects', {
    name: 'Spike Project',
    statement: 'kernel drives the legacy engine',
    source_library_ids: [library.id],
  })

  const paper = await api(ctx, 'POST', `/api/projects/${project.id}/papers`, { bibtex: BIBTEX })

  // Synchronous wiki compile through the deterministic fake librarian.
  const compiled = await api(ctx, 'POST', `/api/papers/${paper.id}/recompile`)
  const wikiMarkdown: string = compiled.wiki?.content ?? compiled.wiki_content ?? ''
  if (!wikiMarkdown) throw new Error(`recompile returned no wiki content: ${JSON.stringify(compiled).slice(0, 300)}`)

  // Per-paper index rebuild is synchronous by design (voyage runs are the ARQ
  // consumers); it exercises chunking + fake embeddings + vector bookkeeping.
  const status = await api(ctx, 'POST', `/api/papers/${paper.id}/index/rebuild`)
  const indexStatus =
    status.paper_vector?.built && status.chunk_vector?.built ? 'built' : JSON.stringify(status)

  return { paperId: paper.id, token: ctx.token!, wikiMarkdown, indexStatus }
}
