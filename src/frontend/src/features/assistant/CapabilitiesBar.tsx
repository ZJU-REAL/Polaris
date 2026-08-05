import { useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   「它现在到底能干什么」——工具面、技能、MCP。

   此前这个问题只能靠读代码回答：工具白名单写在常量里，技能目录只在提示词里出现过，
   对外 MCP 暴露的是不是同一批也没人说得清。

   一条可展开的横条，收起时只占一行。展开后如实列出三样，其中 MCP 那栏**不列
   「已连接的服务器」**——Polaris 自己是 MCP 服务端，没有对外连接。造一个空列表出来
   比不显示更误导。
   ============================================================ */

interface Capabilities {
  tools: { name: string; description: string; read_only: boolean }[];
  skills: { slug: string; name: string; description: string; is_builtin: boolean }[];
  mcp: { role: string; endpoint: string; exposed_tools: number; note: string };
}

export function CapabilitiesBar({ loadedSkills }: { loadedSkills: Set<string> }) {
  const [open, setOpen] = useState(false);
  const [caps, setCaps] = useState<Capabilities | null>(null);

  useEffect(() => {
    let alive = true;
    void api
      .getBuddyCapabilities()
      .then((c) => {
        if (alive) setCaps(c as Capabilities);
      })
      .catch(() => {
        /* 取不到就不显示这条，不摆一个空壳 */
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!caps) return null;

  return (
    <div style={{ borderBottom: '0.5px solid var(--border-2)', background: 'var(--surface-2)' }}>
      <div
        className="row gap8 hoverable"
        onClick={() => setOpen((o) => !o)}
        style={{ padding: '6px 14px', alignItems: 'center', cursor: 'pointer', fontSize: 11.5 }}
      >
        <Icon
          name="chevDown"
          size={11}
          style={{
            color: 'var(--text-4)',
            transform: open ? undefined : 'rotate(-90deg)',
            transition: 'transform 0.12s',
          }}
        />
        <span style={{ color: 'var(--text-3)' }}>
          {tr(`工具 ${caps.tools.length}`, `${caps.tools.length} tools`)}
        </span>
        <span style={{ color: 'var(--text-4)' }}>·</span>
        <span style={{ color: 'var(--text-3)' }}>
          {tr(`技能 ${caps.skills.length}`, `${caps.skills.length} skills`)}
          {loadedSkills.size > 0 && (
            <span style={{ color: 'var(--accent)' }}>
              {tr(`（本轮用了 ${loadedSkills.size}）`, ` (${loadedSkills.size} used)`)}
            </span>
          )}
        </span>
        <span style={{ color: 'var(--text-4)' }}>·</span>
        <span style={{ color: 'var(--text-3)' }}>
          MCP {caps.mcp.exposed_tools}
        </span>
      </div>

      {open && (
        <div style={{ padding: '2px 14px 10px', fontSize: 11.5, lineHeight: 1.6 }}>
          <div style={{ color: 'var(--text-4)', marginBottom: 3 }}>{tr('本轮可用工具', 'Tools this turn')}</div>
          <div className="row gap6 wrap" style={{ marginBottom: 9 }}>
            {caps.tools.map((t) => (
              <span key={t.name} className="pill sm mono" title={t.description}>
                {t.name}
              </span>
            ))}
          </div>

          <div style={{ color: 'var(--text-4)', marginBottom: 3 }}>
            {tr('技能（按需加载，不常驻提示词）', 'Skills (loaded on demand, not resident in the prompt)')}
          </div>
          <div className="col gap6" style={{ marginBottom: 9 }}>
            {caps.skills.length === 0 && (
              <span style={{ color: 'var(--text-4)' }}>{tr('还没有技能', 'No skills yet')}</span>
            )}
            {caps.skills.map((s) => (
              <div key={s.slug} className="row gap6" style={{ alignItems: 'flex-start' }}>
                <Icon
                  name={loadedSkills.has(s.slug) ? 'check' : 'dot'}
                  size={11}
                  style={{
                    marginTop: 4,
                    flexShrink: 0,
                    color: loadedSkills.has(s.slug) ? 'var(--ok-tx)' : 'var(--text-4)',
                  }}
                />
                <div style={{ minWidth: 0 }}>
                  <span className="mono" style={{ color: 'var(--text-2)' }}>
                    {s.slug}
                  </span>
                  {s.is_builtin && (
                    <span style={{ color: 'var(--text-4)' }}>{tr(' · 内置', ' · built-in')}</span>
                  )}
                  <div style={{ color: 'var(--text-4)' }}>{s.description}</div>
                </div>
              </div>
            ))}
          </div>

          <div style={{ color: 'var(--text-4)', marginBottom: 3 }}>MCP</div>
          <div style={{ color: 'var(--text-3)' }}>
            {tr(
              `Polaris 自己是 MCP 服务端（${caps.mcp.endpoint}），对外暴露 ${caps.mcp.exposed_tools} 个只读工具。`,
              `Polaris is itself an MCP server (${caps.mcp.endpoint}) exposing ${caps.mcp.exposed_tools} read-only tools.`,
            )}
            <div style={{ color: 'var(--text-4)' }}>{caps.mcp.note}</div>
          </div>
        </div>
      )}
    </div>
  );
}
