import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   设置里的 PolarisBuddy：技能、记忆、MCP。

   为什么这三样放一起：它们回答的是同一个问题——「Buddy 知道什么、能做什么」。
   技能是按需加载的用法说明，记忆是关于你的长期事实，MCP 是工具面对外的样子。
   分散在三处的话，用户要调整 Buddy 的行为就得先猜去哪儿找。
   ============================================================ */

function SkillsCard() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['buddy-skills'],
    queryFn: () => api.listAssistantSkills(),
    retry: false,
  });
  const remove = useMutation({
    mutationFn: (slug: string) => api.deleteAssistantSkill(slug),
    onSuccess: () => {
      toast(tr('技能已删除', 'Skill deleted'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['buddy-skills'] });
    },
    onError: (e) => toast(e instanceof Error ? e.message : String(e), 'error'),
  });

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError) {
    return (
      <div className="empty">
        {tr('无法加载技能（助手未启用或后端不可用）', 'Cannot load skills (assistant off or backend down)')}
      </div>
    );
  }
  const skills = data ?? [];

  return (
    <div className="card card-pad">
      <div className="section-h" style={{ marginBottom: 6 }}>
        <Icon name="sparkle" size={15} style={{ color: 'var(--accent)' }} />
        {tr('技能', 'Skills')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.7, marginBottom: 10 }}>
        {tr(
          '技能是「什么时候该怎么做」的说明书。目录里每条只占一行常驻，正文由 Buddy 判断需要时才加载——所以描述要写成触发条件，而不是功能介绍。',
          'A skill describes how to handle a kind of task. Only one line per skill stays resident; the body is loaded on demand — so the description should read as a trigger condition, not a feature blurb.',
        )}
      </div>
      {skills.length === 0 && <div className="empty">{tr('还没有技能', 'No skills yet')}</div>}
      <div className="col gap8">
        {skills.map((skill) => (
          <div
            key={skill.slug}
            className="row gap8"
            style={{
              alignItems: 'flex-start',
              padding: '8px 10px',
              border: '0.5px solid var(--border-2)',
              borderRadius: 8,
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="row gap6" style={{ alignItems: 'center' }}>
                <span className="mono" style={{ fontSize: 12.5 }}>
                  {skill.slug}
                </span>
                {skill.is_builtin && (
                  <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
                    {tr('内置', 'built-in')}
                  </span>
                )}
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--text-4)', lineHeight: 1.6, marginTop: 2 }}>
                {skill.description}
              </div>
            </div>
            {/* 内置技能删不掉：它是代码的一部分，改了下次启动又会被种回来。
                与其让按钮点了没反应，不如不给按钮。 */}
            {!skill.is_builtin && (
              <button
                className="btn btn-ghost sm"
                onClick={() => remove.mutate(skill.slug)}
                style={{ height: 24, fontSize: 11 }}
              >
                {tr('删除', 'Delete')}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function MemoryCard() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const { data, isLoading, isError } = useQuery({
    queryKey: ['buddy-memories'],
    queryFn: () => api.listBuddyMemories(),
    retry: false,
  });
  const add = useMutation({
    mutationFn: (text: string) => api.addBuddyMemory(text),
    onSuccess: () => {
      setDraft('');
      void queryClient.invalidateQueries({ queryKey: ['buddy-memories'] });
    },
    onError: (e) => toast(e instanceof Error ? e.message : String(e), 'error'),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBuddyMemory(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['buddy-memories'] }),
  });

  const memories = data ?? [];

  return (
    <div className="card card-pad">
      <div className="section-h" style={{ marginBottom: 6 }}>
        <Icon name="bulb" size={15} style={{ color: 'var(--accent)' }} />
        {tr('长期记忆', 'Memory')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.7, marginBottom: 10 }}>
        {tr(
          '你希望 Buddy 一直记得的事——研究方向、习惯的表达、不想被推荐的东西。每轮对话都会带上，所以宜少而准：太多会挤占它读你问题的余地。目前只有你能写，Buddy 自己往里记属于写工具那一期。',
          'Things you want Buddy to always know. They ride along on every turn, so keep them few and sharp. Only you can write them for now; Buddy writing its own memory comes with the write-tools phase.',
        )}
      </div>
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <input
          className="input"
          value={draft}
          maxLength={300}
          placeholder={tr('例如：我做具身智能，不用给我推纯 NLP 的工作', 'e.g. I work on embodied AI — skip pure NLP')}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && draft.trim()) add.mutate(draft.trim());
          }}
          style={{ flex: 1, minWidth: 0 }}
        />
        <button
          className="btn btn-soft sm"
          disabled={!draft.trim() || add.isPending}
          onClick={() => add.mutate(draft.trim())}
        >
          {tr('记住', 'Remember')}
        </button>
      </div>
      {isLoading && <div className="empty">{tr('加载中…', 'Loading…')}</div>}
      {isError && (
        <div className="empty">
          {tr('无法加载记忆（助手未启用或后端不可用）', 'Cannot load memories (assistant off or backend down)')}
        </div>
      )}
      {!isLoading && !isError && memories.length === 0 && (
        <div className="empty">{tr('还没有记忆', 'Nothing remembered yet')}</div>
      )}
      <div className="col gap6">
        {memories.map((memory) => (
          <div
            key={memory.id}
            className="row gap8"
            style={{
              alignItems: 'flex-start',
              padding: '7px 10px',
              border: '0.5px solid var(--border-2)',
              borderRadius: 8,
              fontSize: 12.5,
              lineHeight: 1.6,
            }}
          >
            <span style={{ flex: 1, minWidth: 0 }}>{memory.text}</span>
            <button
              className="icon-btn"
              title={tr('忘掉这条', 'Forget this')}
              onClick={() => remove.mutate(memory.id)}
            >
              <Icon name="trash" size={12} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function McpCard() {
  const { data } = useQuery({
    queryKey: ['buddy-capabilities'],
    queryFn: () => api.getBuddyCapabilities(),
    retry: false,
  });

  return (
    <div className="card card-pad">
      <div className="section-h" style={{ marginBottom: 6 }}>
        <Icon name="git" size={15} style={{ color: 'var(--accent)' }} />
        MCP
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.7 }}>
        {tr(
          'Polaris 自己是 MCP 服务端：Buddy 用的工具，与 Claude Desktop 这类外部客户端拿到的是同一批（对外只给只读的）。所以这里没有「已连接的服务器」列表——我们不对外连接，列一个空表只会让人误会。',
          'Polaris is itself an MCP server: Buddy uses the same tools external clients (Claude Desktop and the like) receive, minus anything that writes. There is no "connected servers" list because Polaris makes no outbound MCP connections.',
        )}
      </div>
      {data && (
        <div className="row gap8" style={{ marginTop: 10, alignItems: 'center', fontSize: 12 }}>
          <span className="pill sm mono">{data.mcp.endpoint}</span>
          <span style={{ color: 'var(--text-3)' }}>
            {tr(`对外暴露 ${data.mcp.exposed_tools} 个只读工具`, `${data.mcp.exposed_tools} read-only tools exposed`)}
          </span>
          <span style={{ color: 'var(--text-4)' }}>·</span>
          <span style={{ color: 'var(--text-3)' }}>
            {tr(`Buddy 本轮可用 ${data.tools.length} 个`, `${data.tools.length} available to Buddy`)}
          </span>
        </div>
      )}
    </div>
  );
}

export function BuddySettings() {
  return (
    <div className="col gap14">
      <MemoryCard />
      <SkillsCard />
      <McpCard />
    </div>
  );
}
