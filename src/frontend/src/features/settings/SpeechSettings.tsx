import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { api, type TTSAdminSettings, type TTSUserSettingsUpdate } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { SettingsActions, SettingsGroup, SettingsRow, SettingsSection, SettingsStack } from './settingsUi';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function LoadingCard() {
  return <div className="skel" style={{ height: 240 }} />;
}

export function PersonalSpeechSettings() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ['tts-settings'],
    queryFn: () => api.getTtsSettings(),
    retry: false,
  });
  const [draft, setDraft] = useState<TTSUserSettingsUpdate | null>(null);

  useEffect(() => {
    if (!settingsQuery.data || draft) return;
    setDraft({
      enabled: settingsQuery.data.enabled,
      model: settingsQuery.data.model,
      voice: settingsQuery.data.voice,
      speed: settingsQuery.data.speed,
    });
  }, [draft, settingsQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (value: TTSUserSettingsUpdate) => api.setTtsSettings(value),
    onSuccess: (saved) => {
      setDraft({
        enabled: saved.enabled,
        model: saved.model,
        voice: saved.voice,
        speed: saved.speed,
      });
      queryClient.setQueryData(['tts-settings'], saved);
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });

  if (settingsQuery.isLoading) return <LoadingCard />;
  if (settingsQuery.isError || !settingsQuery.data) {
    return <div className="empty">{tr('无法加载语音设置', 'Couldn’t load speech settings')}</div>;
  }
  if (!draft) return <LoadingCard />;
  const settings = settingsQuery.data;

  return (
    <SettingsStack>
      <SettingsSection title={tr('朗读', 'Read aloud')}>
        {!settings.available && settings.enabled && (
          <p className="st-section-desc" style={{ color: 'var(--warn-tx)' }}>
            {tr('语音服务还没开启，请在「模型与智能体」中设置语音模型。', 'Speech isn’t set up yet. Set a speech model under Models & agents.')}
          </p>
        )}
        <SettingsGroup>
          <SettingsRow
            labelId="speech-enabled"
            label={tr('显示朗读按钮', 'Show read-aloud button')}
            hint={tr('在回答和每日简报旁显示，点击时才生成语音', 'On answers and daily digests. Audio is made only when you click.')}
          >
            <Switch
              checked={draft.enabled}
              onChange={(enabled) => setDraft((value) => value ? { ...value, enabled } : value)}
              aria-labelledby="speech-enabled"
            />
          </SettingsRow>
          <SettingsRow label={tr('语音模型', 'Speech model')}>
            <select
              className="input"
              value={draft.model ?? ''}
              onChange={(event) => setDraft((value) => value ? { ...value, model: event.target.value || null } : value)}
            >
              <option value="">{tr(`默认（${settings.effective_model}）`, `Default (${settings.effective_model})`)}</option>
              {settings.available_models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          </SettingsRow>
          <SettingsRow label={tr('音色', 'Voice')}>
            <select
              className="input"
              value={draft.voice ?? ''}
              onChange={(event) => setDraft((value) => value ? { ...value, voice: event.target.value || null } : value)}
            >
              <option value="">{tr(`默认（${settings.effective_voice}）`, `Default (${settings.effective_voice})`)}</option>
              {settings.available_voices.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
            </select>
          </SettingsRow>
          <SettingsRow
            label={tr('语速', 'Speed')}
            hint={draft.speed === null
              ? tr(`默认 ${settings.effective_speed.toFixed(1)}×`, `Default ${settings.effective_speed.toFixed(1)}×`)
              : `${draft.speed.toFixed(1)}×`}
          >
            <input
              type="range"
              min="0.5"
              max="2"
              step="0.1"
              value={draft.speed ?? settings.effective_speed}
              onChange={(event) => setDraft((value) => value ? { ...value, speed: Number(event.target.value) } : value)}
              style={{ width: 180 }}
            />
            <button className="btn btn-ghost sm" disabled={draft.speed === null} onClick={() => setDraft((value) => value ? { ...value, speed: null } : value)}>
              {tr('恢复默认', 'Reset')}
            </button>
          </SettingsRow>
        </SettingsGroup>
        <SettingsActions>
          <button className="btn btn-primary sm" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate(draft)}>
            {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
          </button>
        </SettingsActions>
      </SettingsSection>
    </SettingsStack>
  );
}

export function AdminSpeechSettings() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ['admin-tts-settings'],
    queryFn: () => api.getAdminTtsSettings(),
    retry: false,
  });
  const [draft, setDraft] = useState<TTSAdminSettings | null>(null);

  const voicesQuery = useQuery({
    queryKey: [
      'admin-tts-voices',
      settingsQuery.data?.base_url,
      settingsQuery.data?.model,
    ],
    queryFn: () => api.getAdminTtsVoices(settingsQuery.data!),
    enabled: Boolean(settingsQuery.data),
    retry: false,
  });

  useEffect(() => {
    if (settingsQuery.data && !draft) setDraft(settingsQuery.data);
  }, [draft, settingsQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (value: TTSAdminSettings) => api.setAdminTtsSettings(value),
    onSuccess: (saved) => {
      setDraft(saved);
      queryClient.setQueryData(['admin-tts-settings'], saved);
      void queryClient.invalidateQueries({ queryKey: ['tts-settings'] });
      void queryClient.invalidateQueries({ queryKey: ['admin-tts-voices'] });
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });
  const testMutation = useMutation({
    mutationFn: (value: TTSAdminSettings) => api.testAdminTtsSettings(value),
    onSuccess: () => toast(tr('连接成功', 'Connected'), 'ok'),
    onError: (error) => toast(`${tr('连接失败', 'Connection failed')}：${errorText(error)}`, 'error'),
  });
  const voicesMutation = useMutation({
    mutationFn: (value: TTSAdminSettings) => api.getAdminTtsVoices(value),
    onSuccess: (result) => {
      queryClient.setQueryData(
        ['admin-tts-voices', draft?.base_url, draft?.model],
        result,
      );
      toast(tr(`找到 ${result.voices.length} 个音色`, result.voices.length === 1 ? 'Found 1 voice' : `Found ${result.voices.length} voices`), 'ok');
    },
    onError: (error) => toast(`${tr('无法获取音色', 'Couldn’t load voices')}：${errorText(error)}`, 'error'),
  });

  if (settingsQuery.isLoading) return <LoadingCard />;
  if (settingsQuery.isError || !settingsQuery.data) {
    return <div className="empty">{tr('无法加载语音设置', 'Couldn’t load speech settings')}</div>;
  }
  if (!draft) return <LoadingCard />;

  const patch = <K extends keyof TTSAdminSettings>(key: K, value: TTSAdminSettings[K]) => {
    setDraft((current) => current ? { ...current, [key]: value } : current);
  };
  const discoveredVoices = voicesMutation.data?.voices ?? voicesQuery.data?.voices ?? [];
  const voiceOptions = Array.from(new Set([draft.default_voice, ...discoveredVoices]));

  return (
    <SettingsSection title={tr('语音服务', 'Speech')} desc={tr('用于朗读，需兼容 OpenAI 语音接口。', 'Used for read aloud. Needs an OpenAI-compatible speech API.')}>
      <SettingsGroup>
        <SettingsRow labelId="speech-service-enabled" label={tr('开启语音服务', 'Turn on speech')}>
          <Switch checked={draft.enabled} onChange={(enabled) => patch('enabled', enabled)} aria-labelledby="speech-service-enabled" />
        </SettingsRow>
        <SettingsRow label={tr('API 地址', 'API URL')}>
          <input className="input mono" value={draft.base_url} placeholder={tr('例如 http://localhost:50000/v1', 'e.g. http://localhost:50000/v1')} onChange={(event) => patch('base_url', event.target.value)} />
        </SettingsRow>
        <SettingsRow label={tr('模型', 'Model')}>
          <input className="input mono" value={draft.model} onChange={(event) => patch('model', event.target.value)} />
        </SettingsRow>
        <SettingsRow label={tr('默认音色', 'Default voice')} hint={tr('修改 API 地址后点刷新获取音色', 'Refresh after changing the API URL')}>
          <select
            className="input mono"
            value={draft.default_voice}
            onChange={(event) => patch('default_voice', event.target.value)}
            style={{ width: 200 }}
          >
            {voiceOptions.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
          </select>
          <button
            type="button"
            className="btn btn-ghost sm"
            title={tr('刷新音色', 'Refresh voices')}
            disabled={voicesMutation.isPending || voicesQuery.isFetching}
            onClick={() => voicesMutation.mutate(draft)}
          >
            <Icon name="refresh" size={13} />
          </button>
        </SettingsRow>
        <SettingsRow label={tr('每段最长字数', 'Max characters per segment')} hint={tr('长文本会分段连续播放', 'Longer text plays in segments')}>
          <input className="input mono st-num" type="number" min={200} max={50000} value={draft.max_chars} onChange={(event) => patch('max_chars', Number(event.target.value))} />
        </SettingsRow>
        <SettingsRow label={tr('默认语速', 'Default speed')} hint={`${draft.default_speed.toFixed(1)}×`}>
          <input type="range" min="0.5" max="2" step="0.1" value={draft.default_speed} onChange={(event) => patch('default_speed', Number(event.target.value))} style={{ width: 180 }} />
        </SettingsRow>
      </SettingsGroup>
      <SettingsActions>
        <button className="btn btn-ghost sm" disabled={testMutation.isPending} onClick={() => testMutation.mutate(draft)}>
          {testMutation.isPending ? tr('测试中…', 'Testing…') : tr('测试连接', 'Test connection')}
        </button>
        <button className="btn btn-primary sm" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate(draft)}>
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </SettingsActions>
    </SettingsSection>
  );
}
