import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormField } from '../../components/ui/FormField';
import { Icon } from '../../components/ui/Icon';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { api, type TTSAdminSettings, type TTSUserSettingsUpdate } from '../../lib/api';
import { tr } from '../../lib/i18n';

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
      toast(tr('语音偏好已保存', 'Speech preferences saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${errorText(error)}`, 'error'),
  });

  if (settingsQuery.isLoading) return <LoadingCard />;
  if (settingsQuery.isError || !settingsQuery.data) {
    return <div className="empty">{tr('无法加载语音设置', 'Failed to load speech settings')}</div>;
  }
  if (!draft) return <LoadingCard />;
  const settings = settingsQuery.data;

  return (
    <div className="settings-main-side">
      <div className="card card-pad">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
          <div>
            <div className="section-h">
              <Icon name="play" size={15} style={{ color: 'var(--accent)' }} />
              {tr('AI 语音听读', 'AI read aloud')}
            </div>
            <div style={{ marginTop: 5, fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
              {tr('在 AI 回答与每日简报旁显示播放按钮。语音仅在点击时生成。', 'Show a play button on AI answers and daily digests. Audio is generated only when clicked.')}
            </div>
          </div>
          <Switch
            checked={draft.enabled}
            onChange={(enabled) => setDraft((value) => value ? { ...value, enabled } : value)}
            aria-label={tr('启用语音听读', 'Enable read aloud')}
          />
        </div>

        {!settings.available && settings.enabled && (
          <div className="speech-settings-notice">
            {tr('管理员尚未启用语音服务；你的偏好会保留，服务启用后自动生效。', 'The administrator has not enabled speech yet. Your preference will take effect once it is available.')}
          </div>
        )}

        <div className="settings-fields" style={{ marginTop: 20 }}>
          <FormField label={tr('语音模型', 'Speech model')} hint={tr('“跟随管理员”会自动使用管理员当前指定的模型。', 'Follow admin automatically uses the current platform model.')}>
            <select
              className="input"
              value={draft.model ?? ''}
              onChange={(event) => setDraft((value) => value ? { ...value, model: event.target.value || null } : value)}
            >
              <option value="">{tr('跟随管理员', 'Follow admin')}</option>
              {settings.available_models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          </FormField>
          <FormField label={tr('音色', 'Voice')}>
            <select
              className="input"
              value={draft.voice ?? ''}
              onChange={(event) => setDraft((value) => value ? { ...value, voice: event.target.value || null } : value)}
            >
              <option value="">{tr('跟随管理员', 'Follow admin')}</option>
              {settings.available_voices.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
            </select>
          </FormField>
        </div>

        <FormField
          label={tr('播放语速', 'Speaking speed')}
          hint={draft.speed === null
            ? tr(`当前跟随管理员：${settings.effective_speed.toFixed(1)}×`, `Following admin: ${settings.effective_speed.toFixed(1)}×`)
            : `${draft.speed.toFixed(1)}×`}
        >
          <div className="row gap8" style={{ alignItems: 'center' }}>
            <input
              type="range"
              min="0.5"
              max="2"
              step="0.1"
              value={draft.speed ?? settings.effective_speed}
              onChange={(event) => setDraft((value) => value ? { ...value, speed: Number(event.target.value) } : value)}
              style={{ flex: 1 }}
            />
            <button className="btn btn-ghost sm" disabled={draft.speed === null} onClick={() => setDraft((value) => value ? { ...value, speed: null } : value)}>
              {tr('跟随管理员', 'Follow admin')}
            </button>
          </div>
        </FormField>

        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 18 }}>
          <button className="btn btn-primary" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate(draft)}>
            {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
          </button>
        </div>
      </div>

      <div className="card card-pad speech-settings-summary">
        <div className="section-h">{tr('当前生效', 'Effective configuration')}</div>
        <dl>
          <div><dt>{tr('模型', 'Model')}</dt><dd>{settings.effective_model}</dd></div>
          <div><dt>{tr('音色', 'Voice')}</dt><dd>{settings.effective_voice}</dd></div>
          <div><dt>{tr('最长文本', 'Text limit')}</dt><dd>{settings.max_chars.toLocaleString()} {tr('字', 'chars')}</dd></div>
        </dl>
      </div>
    </div>
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

  useEffect(() => {
    if (settingsQuery.data && !draft) setDraft(settingsQuery.data);
  }, [draft, settingsQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (value: TTSAdminSettings) => api.setAdminTtsSettings(value),
    onSuccess: (saved) => {
      setDraft(saved);
      queryClient.setQueryData(['admin-tts-settings'], saved);
      void queryClient.invalidateQueries({ queryKey: ['tts-settings'] });
      toast(tr('平台语音设置已保存', 'Platform speech settings saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${errorText(error)}`, 'error'),
  });
  const testMutation = useMutation({
    mutationFn: (value: TTSAdminSettings) => api.testAdminTtsSettings(value),
    onSuccess: (result) => toast(tr(`连接成功，生成 ${result.audio_bytes.toLocaleString()} 字节音频`, `Connected; generated ${result.audio_bytes.toLocaleString()} bytes of audio`), 'ok'),
    onError: (error) => toast(`${tr('连接测试失败', 'Connection test failed')}：${errorText(error)}`, 'error'),
  });

  if (settingsQuery.isLoading) return <LoadingCard />;
  if (settingsQuery.isError || !settingsQuery.data) {
    return <div className="empty">{tr('无法加载语音设置', 'Failed to load speech settings')}</div>;
  }
  if (!draft) return <LoadingCard />;

  const patch = <K extends keyof TTSAdminSettings>(key: K, value: TTSAdminSettings[K]) => {
    setDraft((current) => current ? { ...current, [key]: value } : current);
  };

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
        <div>
          <div className="section-h">
            <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
            {tr('平台语音服务', 'Platform speech service')}
          </div>
          <div style={{ marginTop: 5, fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
            {tr('配置 OpenAI Speech 兼容接口。Polaris 会鉴权、清理 Markdown，并缓存生成的 WAV。', 'Configure an OpenAI Speech-compatible endpoint. Polaris authenticates requests, cleans Markdown, and caches generated WAV files.')}
          </div>
        </div>
        <Switch checked={draft.enabled} onChange={(enabled) => patch('enabled', enabled)} aria-label={tr('启用平台语音服务', 'Enable platform speech')} />
      </div>

      <div className="settings-fields" style={{ marginTop: 22 }}>
        <FormField label={tr('接口地址', 'Endpoint')} hint={tr('填写独立 TTS API 的 /v1 地址；Docker 同机部署可使用 http://host.docker.internal:50000/v1。', 'Enter the standalone TTS API /v1 URL; a same-host Docker deployment can use http://host.docker.internal:50000/v1.')}>
          <input className="input mono" value={draft.base_url} onChange={(event) => patch('base_url', event.target.value)} />
        </FormField>
        <FormField label={tr('模型', 'Model')}>
          <input className="input mono" value={draft.model} onChange={(event) => patch('model', event.target.value)} />
        </FormField>
        <FormField label={tr('默认音色', 'Default voice')}>
          <input className="input mono" value={draft.default_voice} onChange={(event) => patch('default_voice', event.target.value)} />
        </FormField>
        <FormField label={tr('单次最长文本', 'Maximum text length')}>
          <input className="input mono" type="number" min={200} max={50000} value={draft.max_chars} onChange={(event) => patch('max_chars', Number(event.target.value))} />
        </FormField>
      </div>

      <FormField label={tr('默认语速', 'Default speed')} hint={`${draft.default_speed.toFixed(1)}×`}>
        <input type="range" min="0.5" max="2" step="0.1" value={draft.default_speed} onChange={(event) => patch('default_speed', Number(event.target.value))} style={{ width: '100%' }} />
      </FormField>

      <div className="row gap8" style={{ justifyContent: 'flex-end', marginTop: 20 }}>
        <button className="btn btn-soft" disabled={testMutation.isPending} onClick={() => testMutation.mutate(draft)}>
          <Icon name="play" size={13} />
          {testMutation.isPending ? tr('生成测试音频…', 'Generating test audio…') : tr('测试连接', 'Test connection')}
        </button>
        <button className="btn btn-primary" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate(draft)}>
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
    </div>
  );
}
