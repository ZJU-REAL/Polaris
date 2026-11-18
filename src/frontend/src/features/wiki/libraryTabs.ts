/* ============================================================
   文献库页的页签与地址（纯函数，便于测试）。

     ?tab=            论文（不写） / find / knowledge / ask
     &view=           知识的子视图：concepts / methods / graph / gaps / digest / notes
     &mode=           问答方式：quick / thorough
     &settings=1      设置抽屉开着
     &paper=<id>      论文页签里选中的那篇

   旧地址的 ?tab=（12 个页签时代）换成新口径，见 LEGACY_TABS。
   ============================================================ */

export type LibraryTab = 'papers' | 'find' | 'knowledge' | 'ask';

export const KNOWLEDGE_VIEWS = ['concepts', 'methods', 'graph', 'gaps', 'digest', 'notes'] as const;
export type KnowledgeView = (typeof KNOWLEDGE_VIEWS)[number];

export type AskMode = 'quick' | 'thorough';

/** 旧页签 → 新位置。外部链接、书签、旧代码里的 ?tab= 都经这里换一次。 */
export const LEGACY_TABS: Record<string, { tab: LibraryTab; view?: KnowledgeView; mode?: AskMode; settings?: boolean }> = {
  discover: { tab: 'find' },
  ingest: { tab: 'find' },
  concepts: { tab: 'knowledge', view: 'concepts' },
  methods: { tab: 'knowledge', view: 'methods' },
  graph: { tab: 'knowledge', view: 'graph' },
  gaps: { tab: 'knowledge', view: 'gaps' },
  digest: { tab: 'knowledge', view: 'digest' },
  notes: { tab: 'knowledge', view: 'notes' },
  chat: { tab: 'ask', mode: 'quick' },
  qa: { tab: 'ask', mode: 'thorough' },
  govern: { tab: 'papers', settings: true },
  settings: { tab: 'papers', settings: true },
};

/** 把一组查询参数换成新口径；不需要换时返回 null。 */
export function normalizeLibraryParams(params: URLSearchParams): URLSearchParams | null {
  const raw = params.get('tab');
  if (!raw) return null;
  if (raw === 'find' || raw === 'knowledge' || raw === 'ask') return null;
  const next = new URLSearchParams(params);
  const legacy = LEGACY_TABS[raw];
  if (legacy) {
    if (legacy.tab === 'papers') next.delete('tab');
    else next.set('tab', legacy.tab);
    if (legacy.view) next.set('view', legacy.view);
    if (legacy.mode) next.set('mode', legacy.mode);
    if (legacy.settings) next.set('settings', '1');
    return next;
  }
  // 'papers' 或认不出的页签：回到论文
  next.delete('tab');
  return next;
}

/** 从地址读出当前状态（认不出的值落到默认）。 */
export function readLibraryParams(params: URLSearchParams): {
  tab: LibraryTab;
  view: KnowledgeView;
  mode: AskMode;
  settings: boolean;
  paper: string | null;
} {
  const raw = params.get('tab');
  const tab: LibraryTab = raw === 'find' || raw === 'knowledge' || raw === 'ask' ? raw : 'papers';
  const rawView = params.get('view') ?? '';
  const view: KnowledgeView = (KNOWLEDGE_VIEWS as readonly string[]).includes(rawView) ? (rawView as KnowledgeView) : 'concepts';
  return {
    tab,
    view,
    mode: params.get('mode') === 'thorough' ? 'thorough' : 'quick',
    settings: params.get('settings') === '1',
    paper: params.get('paper'),
  };
}
