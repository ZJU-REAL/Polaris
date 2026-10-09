import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Switch } from '../../components/ui/Switch';
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack } from './settingsUi';

/* ============================================================
   设置里的助手（PolarisBuddy）：长期记忆。

   曾经还有一张 MCP 卡片，列出端点路径与工具数——那是「MCP」标签页的内容，
   在这里只是重复，而且把接口路径摆给了用户。
   ============================================================ */


function MemoryCard() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const { data, isLoading, isError } = useQuery({
    queryKey: ['buddy-memories'],
    queryFn: () => api.listBuddyMemories(),
    retry: false,
  });
  const caps = useQuery({ queryKey: ['buddy-capabilities'], queryFn: () => api.getBuddyCapabilities(), retry: false });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setBuddyMemoryEnabled(enabled),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['buddy-capabilities'] }),
    onError: (e) => toast(e instanceof Error ? e.message : String(e), 'error'),
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
    <SettingsSection title={tr('长期记忆', 'Memory')} desc={tr('助手回答时会参考这些关于你的事实。', 'The assistant keeps these facts about you in mind.')}>
      <SettingsGroup>
        {/* 开着时 Buddy 才有 remember / recall 两个动作。默认关：一个会自己记东西的
            助手，得先由用户说「可以」。 */}
        <SettingsRow labelId="buddy-memory-auto" label={tr('允许助手自动记住和查找', 'Let the assistant remember and look things up')}>
          <Switch
            checked={caps.data?.memory?.enabled ?? false}
            onChange={(enabled) => toggle.mutate(enabled)}
            aria-labelledby="buddy-memory-auto"
          />
        </SettingsRow>
        <div className="st-row">
          <input
            className="input"
            value={draft}
            maxLength={300}
            placeholder={tr('例如：我研究具身智能，不用推荐纯 NLP 的论文', 'e.g. I work on embodied AI. Skip pure NLP papers.')}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && draft.trim()) add.mutate(draft.trim());
            }}
            style={{ flex: 1, minWidth: 0 }}
          />
          <button
            className="btn btn-primary sm"
            disabled={!draft.trim() || add.isPending}
            onClick={() => add.mutate(draft.trim())}
          >
            {tr('记住', 'Remember')}
          </button>
        </div>
        {isLoading && <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>}
        {isError && <div className="st-row"><span className="st-row-hint">{tr('无法加载记忆', 'Couldn’t load memories')}</span></div>}
        {!isLoading && !isError && memories.length === 0 && (
          <div className="st-row"><span className="st-row-hint">{tr('还没有记忆', 'Nothing remembered yet')}</span></div>
        )}
        {memories.map((memory) => (
          <div key={memory.id} className="st-row">
            <span style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.55 }}>{memory.text}</span>
            <button
              className="icon-btn st-quiet-danger"
              title={tr('删除', 'Delete')}
              aria-label={tr('删除', 'Delete')}
              onClick={() => remove.mutate(memory.id)}
            >
              <Icon name="trash" size={12} />
            </button>
          </div>
        ))}
      </SettingsGroup>
    </SettingsSection>
  );
}

export function BuddySettings() {
  return (
    <SettingsStack>
      <MemoryCard />
    </SettingsStack>
  );
}
