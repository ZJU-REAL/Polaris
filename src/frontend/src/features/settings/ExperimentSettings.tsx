import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { ApiError, api, type ExperimentEnvSettings, type ExperimentEnvVar } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   实验设置（admin，全局一份，所有实验共用）

   实验跑在别的机器上，平台并不知道那台机器长什么样——数据放在哪、要不要走代理、
   有哪些软件装在哪。以前这些只能靠提示词里的例子让模型去猜，猜错的代价是整个任务
   跑到冒烟测试才失败。这里配一次，之后写进每个实验的 env.sh，并作为**事实**进代码
   生成的提示词。

   以前五个字段一字排开，模型位置、pip 镜像、HF 端点打头——只有做 Python 机器学习
   的人用得上。现在分两层：通用的（代理、数据位置、自定义环境变量）在上面；Python /
   机器学习专用的收进下面的折叠区，配过才默认展开。别的领域要告诉实验机的事（软件
   装在哪、许可证服务器、项目名）用自定义环境变量表达。
   ============================================================ */

const EMPTY: ExperimentEnvSettings = {
  model_root: '',
  dataset_root: '',
  pip_index_url: '',
  hf_endpoint: '',
  proxy_url: '',
  env_vars: [],
};

type TextKey = Exclude<keyof ExperimentEnvSettings, 'env_vars'>;

/** 后端 422 的 detail 形如 INVALID_EXPERIMENT_SETTING:model_root，取出字段名。 */
function invalidField(e: unknown): string | null {
  if (e instanceof ApiError && e.message.startsWith('INVALID_EXPERIMENT_SETTING:')) {
    return e.message.split(':')[1] ?? null;
  }
  return null;
}

interface TextField {
  key: TextKey;
  zh: string;
  en: string;
  placeholder: string;
  hintZh: string;
  hintEn: string;
}

/** 不管做什么实验都用得上的。 */
const GENERAL_FIELDS: TextField[] = [
  {
    key: 'proxy_url',
    zh: '出网代理',
    en: 'Outbound proxy',
    placeholder: 'http://10.0.0.1:7890',
    hintZh: '实验机出外网用的 HTTP 代理。某台机器在 SSH 凭据里单独配了代理的，以凭据为准。',
    hintEn: 'HTTP proxy for the experiment host. A proxy set on an individual SSH credential takes precedence.',
  },
  {
    key: 'dataset_root',
    zh: '数据位置',
    en: 'Data directory',
    placeholder: '/data',
    hintZh: '实验机上放数据的根目录（环境变量 $POLARIS_DATASET_ROOT）。生成的代码按这个位置找数据，不再靠猜路径。',
    hintEn: 'Where data lives on the experiment host ($POLARIS_DATASET_ROOT). Generated code looks for data here instead of guessing.',
  },
];

/** 只有 Python / 机器学习实验用得上的。 */
const PYTHON_FIELDS: TextField[] = [
  {
    key: 'model_root',
    zh: '模型位置',
    en: 'Model directory',
    placeholder: '/hf/model',
    hintZh: '实验机上本地模型的存放根目录（$POLARIS_MODEL_ROOT）。',
    hintEn: 'Where local models live on the experiment host ($POLARIS_MODEL_ROOT).',
  },
  {
    key: 'pip_index_url',
    zh: 'pip 镜像源',
    en: 'pip index URL',
    placeholder: 'https://pypi.tuna.tsinghua.edu.cn/simple',
    hintZh: '装 Python 依赖走这个源。连不上官方源的机器建议配上。',
    hintEn: 'Python dependency installs use this index. Set it if the host cannot reach PyPI.',
  },
  {
    key: 'hf_endpoint',
    zh: 'HuggingFace 端点',
    en: 'HuggingFace endpoint',
    placeholder: 'https://hf-mirror.com',
    hintZh: '从 HuggingFace 拉模型/数据集时用的地址。',
    hintEn: 'Endpoint used when pulling models or datasets from HuggingFace.',
  },
];

const ALL_FIELDS = [...GENERAL_FIELDS, ...PYTHON_FIELDS];

/** 与后端 experiment_settings.RESERVED_ENV_NAMES 同一份：平台自己导出或 shell 离不开的。 */
const RESERVED_ENV_NAMES = new Set([
  'POLARIS_WORKDIR', 'POLARIS_MODEL_ROOT', 'POLARIS_DATASET_ROOT', 'PIP_INDEX_URL', 'HF_ENDPOINT',
  'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'PATH', 'HOME', 'SHELL', 'USER', 'PWD', 'VIRTUAL_ENV',
]);
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
// eslint-disable-next-line no-control-regex
const CONTROL_CHAR_RE = /[\x00-\x1f\x7f]/;

/** 一行自定义变量的问题；没问题返回 null。名字留空的行保存时直接丢掉，不算错。 */
export function envVarIssue(v: ExperimentEnvVar, all: ExperimentEnvVar[]): string | null {
  const name = v.name.trim();
  if (!name) return null;
  if (!ENV_NAME_RE.test(name)) {
    return tr('名字只能用字母、数字和下划线，且不能以数字开头', 'Letters, digits and underscores only; cannot start with a digit');
  }
  if (RESERVED_ENV_NAMES.has(name.toUpperCase())) {
    return tr('平台自己会设置这个变量，换个名字', 'The platform sets this variable itself — pick another name');
  }
  if (all.filter((x) => x.name.trim() === name).length > 1) return tr('名字重复了', 'Duplicate name');
  if (CONTROL_CHAR_RE.test(v.value)) return tr('值里不能有换行', 'Values cannot contain line breaks');
  return null;
}

/** 保存用：去掉名字为空的行、去掉名字两端空白。 */
export function envVarsPayload(rows: ExperimentEnvVar[]): ExperimentEnvVar[] {
  return rows.filter((r) => r.name.trim()).map((r) => ({ name: r.name.trim(), value: r.value }));
}

/** Python 折叠区里配了几项——配过就默认展开，免得人以为没配。 */
export function pythonConfiguredCount(s: ExperimentEnvSettings): number {
  return PYTHON_FIELDS.filter((f) => s[f.key].trim()).length;
}

function normalize(s: ExperimentEnvSettings): ExperimentEnvSettings {
  return { ...EMPTY, ...s, env_vars: envVarsPayload(s.env_vars ?? []) };
}

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
    if (data && draft === null) setDraft({ ...EMPTY, ...data, env_vars: data.env_vars ?? [] });
  }, [data, draft]);

  const saved = { ...EMPTY, ...data, env_vars: data?.env_vars ?? [] };
  const shown = draft ?? saved;
  const dirty = !!data && JSON.stringify(normalize(shown)) !== JSON.stringify(normalize(saved));
  const envIssues = shown.env_vars.map((v) => envVarIssue(v, shown.env_vars));
  const hasEnvIssue = envIssues.some(Boolean);

  const saveMutation = useMutation({
    mutationFn: () => api.setExperimentEnv(normalize(shown)),
    onSuccess: (res) => {
      setBadField(null);
      setDraft({ ...EMPTY, ...res });
      queryClient.setQueryData(['experiment-env'], res);
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

  const renderField = (f: TextField) => (
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
  );

  const setEnvVars = (env_vars: ExperimentEnvVar[]) => {
    setBadField(null);
    setDraft({ ...shown, env_vars });
  };
  const pythonCount = pythonConfiguredCount(saved);

  return (
    <>
    <div className="card card-pad">
      <div className="section-h" style={{ marginBottom: 4 }}>
        <Icon name="flask" size={15} style={{ color: 'var(--accent)' }} />
        {tr('实验环境', 'Experiment environment')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6, marginBottom: 16 }}>
        {tr(
          '所有实验共用这一份。实验跑在另一台机器上，平台不知道那台机器的情况——这里配的会写进每个实验的启动环境，也会作为事实告诉写代码的模型，免得它猜错。留空表示不配置。',
          'Shared by every experiment. Experiments run on another machine the platform knows nothing about — what you set here goes into each run’s environment and is stated as fact to the model that writes the code, so it does not guess. Leave a field empty to skip it.',
        )}
      </div>

      <div className="settings-fields">{GENERAL_FIELDS.map(renderField)}</div>

      <div className="field">
        <label className="field-label">{tr('自定义环境变量', 'Custom environment variables')}</label>
        <div className="col" style={{ gap: 8 }}>
          {shown.env_vars.map((v, i) => (
            <div key={i}>
              <div className="row gap8" style={{ alignItems: 'center' }}>
                <input
                  className="input mono"
                  style={{ flex: '0 1 220px', minWidth: 0 }}
                  value={v.name}
                  placeholder="NAME"
                  spellCheck={false}
                  autoComplete="off"
                  aria-label={tr('变量名', 'Variable name')}
                  onChange={(e) => setEnvVars(shown.env_vars.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                />
                <span style={{ color: 'var(--text-3)' }}>=</span>
                <input
                  className="input mono"
                  style={{ flex: 1, minWidth: 0 }}
                  value={v.value}
                  placeholder={tr('值', 'value')}
                  spellCheck={false}
                  autoComplete="off"
                  aria-label={tr('变量值', 'Variable value')}
                  onChange={(e) => setEnvVars(shown.env_vars.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                />
                <button
                  className="btn btn-ghost sm"
                  title={tr('删除这一行', 'Remove this row')}
                  onClick={() => setEnvVars(shown.env_vars.filter((_, j) => j !== i))}
                >
                  <Icon name="x" size={12} />
                </button>
              </div>
              {envIssues[i] && <div className="field-error">{envIssues[i]}</div>}
            </div>
          ))}
          <div>
            <button className="btn btn-soft sm" onClick={() => setEnvVars([...shown.env_vars, { name: '', value: '' }])}>
              <Icon name="plus" size={12} />
              {tr('添加变量', 'Add variable')}
            </button>
          </div>
        </div>
        {badField === 'env_vars' ? (
          <div className="field-error">{tr('有变量名或值不合法', 'A variable name or value is invalid')}</div>
        ) : (
          <div className="field-hint">
            {tr(
              '每个实验启动前都会导出，用来告诉实验机任何事：软件装在哪（如 MATLAB_ROOT）、许可证服务器、项目名。写代码的模型只看得到变量名、看不到值，会从环境里读取。',
              'Exported before every run — use them for anything the host needs: where software is installed (e.g. MATLAB_ROOT), a license server, a project name. The model writing the code sees only the names, never the values, and reads them from the environment.',
            )}
          </div>
        )}
      </div>

      {/* 配过才默认展开：key 随「配没配」变化，保存后按新状态重新决定开合 */}
      <details key={pythonCount > 0 ? 'open' : 'closed'} open={pythonCount > 0} className="settings-disclosure">
        <summary>
          {tr('Python / 机器学习', 'Python / machine learning')}
          <span className="settings-disclosure-note">
            {pythonCount > 0
              ? tr(`已配 ${pythonCount} 项`, `${pythonCount} set`)
              : tr('模型目录、pip 镜像、HuggingFace 端点', 'Model directory, pip index, HuggingFace endpoint')}
          </span>
        </summary>
        <div className="settings-fields" style={{ marginTop: 12 }}>{PYTHON_FIELDS.map(renderField)}</div>
      </details>

      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
        <button
          className="btn btn-primary"
          disabled={!dirty || hasEnvIssue || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
    </div>
    <ManagedCommandWatchdogAdminCard />
    </>
  );
}

function labelOf(field: string): string {
  if (field === 'env_vars') return tr('自定义环境变量', 'Custom environment variables');
  const f = ALL_FIELDS.find((x) => x.key === field);
  return f ? tr(f.zh, f.en) : field;
}

function ManagedCommandWatchdogAdminCard() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['managed-command-watchdog', 'admin'],
    queryFn: () => api.getManagedCommandWatchdog(),
    retry: false,
  });
  const [minutes, setMinutes] = useState<number | null>(null);
  const shown = minutes ?? query.data?.max_unanswered_minutes ?? 120;
  const mutation = useMutation({
    mutationFn: () => api.setManagedCommandWatchdog(shown),
    onSuccess: (saved) => {
      setMinutes(saved.max_unanswered_minutes);
      queryClient.setQueryData(['managed-command-watchdog', 'admin'], saved);
      void queryClient.invalidateQueries({ queryKey: ['managed-command-watchdog', 'user'] });
      toast(tr('无人答复策略已保存', 'Unanswered-command policy saved'), 'ok');
    },
    onError: (error) => toast(
      `${tr('保存失败', 'Save failed')}：${error instanceof Error ? error.message : String(error)}`,
      'error',
    ),
  });
  return (
    <div className="card card-pad" style={{ marginTop: 16 }}>
      <div className="section-h" style={{ marginBottom: 4 }}>
        <Icon name="clock" size={15} style={{ color: 'var(--accent)' }} />
        {tr('远端命令无人答复策略', 'Unanswered remote-command policy')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6, marginBottom: 16 }}>
        {tr(
          '当远端命令超时后转为等待用户决定，此值是允许等待的最长时间。到期后仅在确认该命令本身仍占用 GPU 时自动终止；未占用 GPU 或无法可靠归属时继续保留。用户可以选择更短时间，但不能超过此上限。',
          'Maximum wait after a timed-out remote command asks for a decision. Once reached, Polaris stops it only when GPU use is attributable to that command; idle or uncertain commands remain running. Users may choose a shorter wait, never a longer one.',
        )}
      </div>
      {query.isError ? (
        <div className="empty">{tr('无法加载设置', 'Failed to load settings')}</div>
      ) : (
        <FormField
          label={tr('最长等待时间（分钟）', 'Maximum wait (minutes)')}
          hint={tr('范围 15 分钟至 7 天，默认 120 分钟。', '15 minutes to 7 days; default 120 minutes.')}
        >
          <input
            className="input mono"
            type="number"
            min={15}
            max={10080}
            value={shown}
            onChange={(event) => setMinutes(Number(event.target.value))}
          />
        </FormField>
      )}
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 6 }}>
        <button
          className="btn btn-primary"
          disabled={query.isLoading || shown < 15 || shown > 10080 || mutation.isPending || shown === query.data?.max_unanswered_minutes}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
    </div>
  );
}
