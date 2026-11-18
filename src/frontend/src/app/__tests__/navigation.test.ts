import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { groupActivity, waitingCount } from '../ActivityDrawer';
import { crumbsFor, navSectionOf } from '../crumbs';
import { TOPIC_TABS, topicTabOf } from '../DirectionNav';
import { mergeDirections } from '../../features/deepresearch/directions';
import { expProgress } from '../../features/experiment/shared';
import type { ProjectRead, VoyageRead } from '../../lib/api';
import type { ResearchRunRead } from '../../lib/researchApi';

/* 一个结构：侧栏 = 研究（课题）· 论文 · 每日新论文 · 设置；课题的页面从课题页头的页签进。 */

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf-8');

const project = (id: string, name: string, updated = '2026-11-01T00:00:00Z'): ProjectRead => ({
  id, name, slug: id, statement: null, status: 'active', research_mode: 'conventional',
  owner_id: 'u', created_at: updated, updated_at: updated,
});
const run = (id: string, projectId: string, over: Partial<ResearchRunRead> = {}): ResearchRunRead => ({
  id, project_id: projectId, library_id: `lib-${projectId}`, name: `run ${projectId}`, direction: 'd',
  phase: 'survey', status: 'running', attention: null, counts: { papers: 0, ideas: 0, picked: 0 },
  created_at: '2026-11-10T00:00:00Z', updated_at: '2026-11-10T00:00:00Z', ...over,
});
const voyage = (id: string, over: Partial<VoyageRead>): VoyageRead => ({
  id, kind: 'idea_forge', mode: 'pipeline', goal: 'g', status: 'executing', plan_iteration: 0, plan: null,
  cursor: null, budget: null, usage: null, project_id: null, library_id: null, created_by: null,
  created_at: '2026-11-10T00:00:00Z', updated_at: '2026-11-10T00:00:00Z', ...over,
});

describe('topics in one list', () => {
  it('lists topics with a research run and topics created the old way together', () => {
    const list = mergeDirections([project('a', 'A'), project('b', 'B')], [run('r1', 'a')]);
    expect(list.map((d) => [d.projectId, d.href])).toEqual([
      ['a', '/research/r1'], // 有研究进展：概览就是时间线
      ['b', '/t/b'], // 没有：课题概览
    ]);
  });

  it('puts what is waiting for you first, then what is running', () => {
    const list = mergeDirections(
      [project('a', 'A'), project('b', 'B'), project('c', 'C', '2026-11-17T00:00:00Z')],
      [run('r1', 'a'), run('r2', 'b', { phase: 'choose', status: 'waiting_user' })],
    );
    expect(list.map((d) => d.projectId)).toEqual(['b', 'a', 'c']);
  });

  it('uses the run name for a topic that has one', () => {
    const [d] = mergeDirections([project('a', 'provisional')], [run('r1', 'a', { name: 'Final name' })]);
    expect(d?.name).toBe('Final name');
  });
});

describe('topic tabs', () => {
  it('knows which tab a path is', () => {
    expect(topicTabOf('/research/abc')).toBe('overview');
    expect(topicTabOf('/t/abc')).toBe('overview');
    expect(topicTabOf('/t/abc/forge')).toBe('forge');
    expect(topicTabOf('/t/abc/tasks')).toBe('tasks');
    expect(topicTabOf('/t/abc/settings')).toBe('settings');
    expect(topicTabOf('/research')).toBeNull();
    expect(topicTabOf('/libraries')).toBeNull();
  });

  it('has a route for every tab', () => {
    const routes = read('../routes.tsx');
    for (const tab of TOPIC_TABS) {
      if (tab.sub) expect(routes).toContain(`path: '${tab.sub}'`);
    }
  });
});

describe('breadcrumbs follow the sidebar', () => {
  const topic = { id: 'abc', name: 'Protein design', href: '/research/r1' };

  it('puts every topic page under Research › topic', () => {
    expect(crumbsFor('/research', { topic: null }).map((c) => c.label)).toEqual(['研究']);
    expect(crumbsFor('/research/r1', { topic }).map((c) => c.label)).toEqual(['研究', 'Protein design']);
    expect(crumbsFor('/t/abc/forge', { topic }).map((c) => c.label)).toEqual(['研究', 'Protein design', '想法']);
    expect(crumbsFor('/ideas/i1', { topic }).map((c) => c.label)).toEqual(['研究', 'Protein design', '想法', '想法详情']);
    expect(crumbsFor('/voyages/v1', { topic }).map((c) => c.label)).toEqual(['研究', 'Protein design', '任务', '任务详情']);
  });

  it('links the topic crumb to its overview and the last crumb nowhere', () => {
    const trail = crumbsFor('/t/abc/experiment', { topic });
    expect(trail[1]?.to).toBe('/research/r1');
    expect(trail[2]?.to).toBeUndefined();
  });

  it('puts tasks that belong to no topic under Activity', () => {
    expect(crumbsFor('/voyages/v1', { topic: null, taskOutsideTopic: true }).map((c) => c.label)).toEqual([
      '任务与待办', '任务详情',
    ]);
  });

  it('puts libraries, my library and reading under Papers', () => {
    expect(crumbsFor('/libraries', { topic: null }).map((c) => c.label)).toEqual(['论文', '文献库']);
    expect(crumbsFor('/libraries/x', { topic: null, libraryName: 'RAG' }).map((c) => c.label)).toEqual(['论文', 'RAG']);
    expect(crumbsFor('/library', { topic: null }).map((c) => c.label)).toEqual(['论文', '我的文献库']);
    expect(crumbsFor('/papers/p/read', { topic: null }).map((c) => c.label)).toEqual(['论文', '阅读']);
  });

  it('highlights the right sidebar entry', () => {
    expect(navSectionOf('/research')).toBe('research');
    expect(navSectionOf('/t/abc/writer')).toBe('research');
    expect(navSectionOf('/ideas/i1')).toBe('research');
    expect(navSectionOf('/library')).toBe('papers');
    expect(navSectionOf('/concepts/c1')).toBe('papers');
    expect(navSectionOf('/daily')).toBe('daily');
    expect(navSectionOf('/voyages/v1', true)).toBe('activity');
    expect(navSectionOf('/voyages/v1', false)).toBe('research');
  });
});

describe('the shell no longer carries the old structure', () => {
  const shell = read('../AppShell.tsx');
  const routes = read('../routes.tsx');

  it('has no topic switcher, workbench or numbered stage entries in the sidebar', () => {
    for (const gone of ['TopicSwitcher', '课题工作台', 'Topic Workbench', "no: '01'", '文献任务', 'UserMenu']) {
      expect(shell).not.toContain(gone);
    }
  });

  it('keeps old addresses working', () => {
    for (const old of ["path: 'lab'", "path: 'start'", "path: 'projects/:id'", "path: 'voyages'", "path: 'wiki'", "path: 'admin'"]) {
      expect(routes).toContain(old);
    }
  });

  it('does not link into redirect-only routes from live code', () => {
    const files = [
      '../../features/forge/IdeaDetailPage.tsx',
      '../../features/voyages/shared/StepCard.tsx',
      '../../features/reading/InfoPanel.tsx',
      '../../features/voyages/VoyageDetailPage.tsx',
      '../../features/research/ResearchPage.tsx',
    ];
    for (const f of files) {
      const src = read(f);
      expect(src, f).not.toMatch(/wiki\?(paper|author|affiliation)=/);
      expect(src, f).not.toContain("'/lab'");
      expect(src, f).not.toContain('/projects/${');
    }
  });
});

describe('activity', () => {
  const directions = mergeDirections(
    [project('a', 'A'), project('b', 'B')],
    [run('r1', 'a', { phase: 'choose', status: 'waiting_user' }), run('r2', 'b')],
  );

  it('groups what waits for you, what runs and what finished', () => {
    const data = groupActivity(directions, [
      voyage('v1', { status: 'paused_ask', project_id: 'zzz' }),
      voyage('v2', { status: 'executing', project_id: 'zzz' }),
      voyage('v3', { status: 'done', project_id: 'zzz' }),
      voyage('v4', { status: 'paused_error', project_id: 'zzz' }),
      voyage('v5', { status: 'paused_error', project_id: 'zzz', retry_at: '2026-11-18T10:00:00Z' }),
    ]);
    expect(data.waitingDirections.map((d) => d.projectId)).toEqual(['a']);
    expect(data.runningDirections.map((d) => d.projectId)).toEqual(['b']);
    expect(data.waitingTasks.map((v) => v.id).sort()).toEqual(['v1', 'v4']);
    // 排了自动重试的不算「等你」：它自己会再试
    expect(data.runningTasks.map((v) => v.id).sort()).toEqual(['v2', 'v5']);
    expect(data.recentTasks.map((v) => v.id)).toEqual(['v3']);
    expect(waitingCount(data, 2)).toBe(1 + 2 + 2);
  });

  it('does not list a run and its own survey or hypotheses task as two things', () => {
    const data = groupActivity(directions, [
      voyage('s', { kind: 'wiki_bootstrap', library_id: 'lib-b', status: 'executing' }),
      voyage('h', { kind: 'discovery', project_id: 'a', status: 'paused_ask' }),
      voyage('x', { kind: 'experiment', project_id: 'b', status: 'executing' }),
    ]);
    expect(data.runningTasks.map((v) => v.id)).toEqual(['x']);
    expect(data.waitingTasks).toEqual([]);
  });
});

describe('experiment progress', () => {
  it('does not draw an experiment that is waiting for an answer as finished', () => {
    expect(expProgress('waiting_user')).toBeLessThan(100);
    expect(expProgress('waiting_user')).toBe(expProgress('setup'));
    expect(expProgress('done')).toBe(100);
  });
});
