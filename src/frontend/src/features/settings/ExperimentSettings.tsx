import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { ApiError, api, type ExperimentEnvSettings } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   实验设置（admin，全局一份，所有实验共用）

   实验跑在别的机器上，平台并不知道那台机器长什么样——模型放在哪、有没有 pip 镜像、
   要不要走代理。以前这些只能靠提示词里的例子让模型去猜，猜错的代价是整个任务跑到
   冒烟测试才失败。这里配一次，之后写进每个实验的 env.sh，并作为**事实**进代码生成
   的提示词。
   ============================================================ */

const EMPTY: ExperimentEnvSettings = {
  model_root: '',
  dataset_root: '',
  pip_index_url: '',
  hf_endpoint: '',
  proxy_url: '',
};

/** 后端 422 的 detail 形如 INVALID_EXPERIMENT_SETTING:model_root，取出字段名。 */
function invalidField(e: unknown): string | null {
  if (e instanceof ApiError && e.message.startsWith('INVALID_EXPERIMENT_SETTING:')) {
    return e.message.split(':')[1] ?? null;
  }
  return null;
}

const FIELDS: {
  key: keyof ExperimentEnvSettings;
  zh: string;
  en: string;
  placeholder: string;
  hintZh: string;
  hintEn: string;
}[] = [
  {
    key: 'model_root',
    zh: '模型位置',
    en: 'Model root',
    placeholder: '/hf/model',
    hintZh: '实验机上本地模型的存放根目录。填了之后，生成的代码会按这个前缀引用模型，不再靠猜路径。',
    hintEn: 'Where local models live on the experiment host. Generated code references models under this prefix instead of guessing.',
  },
  {
    key: 'dataset_root',
    zh: '数据集位置',
    en: 'Dataset root',
    placeholder: '/hf/dataset',
    hintZh: '实验机上本地数据集的存放根目录。',
    hintEn: 'Where local datasets live on the experiment host.',
  },
  {
    key: 'pip_index_url',
    zh: 'pip 镜像源',
    en: 'pip index URL',
    placeholder: 'https://pypi.tuna.tsinghua.edu.cn/simple',
    hintZh: '装依赖走这个源。连不上官方源是实验起不来的常见原因，建议配上。',
    hintEn: 'Dependency installs use this index. Unreachable upstream PyPI is a common reason experiments fail to start.',
  },
  {
    key: 'hf_endpoint',
    zh: 'HF 端点',
    en: 'HF endpoint',
    placeholder: 'https://hf-mirror.com',
    hintZh: '从 HuggingFace 拉模型/数据集时用的地址。',
    hintEn: 'Endpoint used when pulling models or datasets from HuggingFace.',
  },
  {
    key: 'proxy_url',
    zh: '出网代理',
    en: 'Outbound proxy',
    placeholder: 'http://10.205.2.46:7899',
    hintZh: '实验机出外网用的 HTTP 代理。某台机器在 SSH 凭据里单独配了代理的，以凭据为准。',
    hintEn: 'HTTP proxy for the experiment host. A proxy set on an individual SSH credential takes precedence.',
  },
];

export function ExperimentSettings() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['experiment-env'],
    queryFn: () => api.getExperimentEnv(),
    retry: false,
  });

  // 本地编辑副本：首次拿到数据后接管，避免 refetch 覆盖未保存的改动
  const [draft, setDraft] = useState<ExperimentEnvSettings | null>(null);
  const [badField, setBadField] = useState<string | null>(null);
  useEffect(() => {
    if (data && draft === null) setDraft({ ...EMPTY, ...data });
  }, [data, draft]);

  const shown = draft ?? data ?? EMPTY;
  const dirty = !!data && JSON.stringify(shown) !== JSON.stringify({ ...EMPTY, ...data });

  const saveMutation = useMutation({
    mutationFn: () => api.setExperimentEnv(shown),
    onSuccess: (saved) => {
      setBadField(null);
      setDraft({ ...EMPTY, ...saved });
      queryClient.setQueryData(['experiment-env'], saved);
      toast(tr('实验设置已保存', 'Experiment settings saved'), 'ok');
    },
    onError: (e) => {
      const field = invalidField(e);
      setBadField(field);
      toast(
        field
          ? tr(`「${labelOf(field)}」格式不对，没保存`, `Invalid ${field} — nothing saved`)
          : `${tr('保存失败', 'Save failed')}：${e instanceof Error ? e.message : String(e)}`,
        'error',
      );
    },
  });

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError) {
    return (
      <div className="empty">
        {tr('无法加载实验设置（后端不可用）', 'Failed to load experiment settings (backend unavailable)')}
        <div style={{ marginTop: 10 }}>
          <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
        </div>
      </div>
    );
  }

  return (
    <div className="card card-pad">
      <div className="section-h" style={{ marginBottom: 4 }}>
        <Icon name="flask" size={15} style={{ color: 'var(--accent)' }} />
        {tr('实验环境', 'Experiment environment')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6, marginBottom: 16 }}>
        {tr(
          '所有实验共用这一份。实验跑在别的机器上，平台不知道那台机器长什么样——配在这里之后，这些会写进每个实验的启动环境，也会作为事实告诉写代码的模型，省得它猜错路径把整个任务跑废。留空表示不配置。',
          'Shared by every experiment. Experiments run on another machine that the platform knows nothing about — what you set here is written into each run’s environment and stated as fact to the model that writes the code, so it does not guess a wrong path and waste the whole run. Leave a field empty to skip it.',
        )}
      </div>

      <div className="settings-fields">
        {FIELDS.map((f) => (
          <FormField
            key={f.key}
            label={tr(f.zh, f.en)}
            hint={tr(f.hintZh, f.hintEn)}
            error={badField === f.key ? tr('格式不对', 'Invalid format') : null}
          >
            <input
              className="input mono"
              value={shown[f.key]}
              placeholder={f.placeholder}
              spellCheck={false}
              autoComplete="off"
              onChange={(e) => {
                setBadField(null);
                setDraft({ ...shown, [f.key]: e.target.value });
              }}
            />
          </FormField>
        ))}
      </div>

      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 6 }}>
        <button
          className="btn btn-primary"
          disabled={!dirty || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
    </div>
  );
}

function labelOf(field: string): string {
  return FIELDS.find((f) => f.key === field)?.zh ?? field;
}
