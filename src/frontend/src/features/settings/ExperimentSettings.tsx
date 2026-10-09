import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { SettingsActions, SettingsGroup, SettingsRow, SettingsSection, SettingsStack } from './settingsUi';
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
    zh: '外网代理',
    en: 'Internet proxy',
    placeholder: 'http://10.0.0.1:7890',
    hintZh: '远程服务器上网用的代理，单台服务器设置了代理时以它为准',
    hintEn: 'Used by remote servers to reach the internet. A server’s own proxy takes priority.',
  },
  {
    key: 'dataset_root',
    zh: '数据位置',
    en: 'Data directory',
    placeholder: '/data',
    hintZh: '服务器上存放数据的目录，实验代码会从这里读取数据',
    hintEn: 'Where data lives on the server. Experiment code reads data from here.',
  },
];

/** 只有 Python / 机器学习实验用得上的。 */
const PYTHON_FIELDS: TextField[] = [
  {
    key: 'model_root',
    zh: '模型位置',
    en: 'Model directory',
    placeholder: '/hf/model',
    hintZh: '服务器上存放模型的目录',
    hintEn: 'Where models live on the server.',
  },
  {
    key: 'pip_index_url',
    zh: 'pip 镜像源',
    en: 'pip index URL',
    placeholder: 'https://pypi.tuna.tsinghua.edu.cn/simple',
    hintZh: '服务器无法访问 PyPI 时填写',
    hintEn: 'Set this if the server can’t reach PyPI.',
  },
  {
    key: 'hf_endpoint',
    zh: 'HuggingFace 端点',
    en: 'HuggingFace endpoint',
    placeholder: 'https://hf-mirror.com',
    hintZh: '下载 HuggingFace 模型和数据集时使用',
    hintEn: 'Used to download HuggingFace models and datasets.',
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
    return tr('只能用字母、数字和下划线，且不能以数字开头', 'Use letters, digits and underscores, and don’t start with a digit');
  }
  if (RESERVED_ENV_NAMES.has(name.toUpperCase())) {
    return tr('这个名字由 Polaris 使用，请换一个', 'Polaris uses this name. Choose another.');
  }
  if (all.filter((x) => x.name.trim() === name).length > 1) return tr('名字重复', 'Duplicate name');
  if (CONTROL_CHAR_RE.test(v.value)) return tr('值不能包含换行', 'Values can’t contain line breaks');
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
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (e) => {
      const field = invalidField(e);
      setBadField(field);
      toast(
        field
          ? tr(`「${labelOf(field)}」格式不对，未保存`, `${labelOf(field)} isn’t valid. Nothing was saved.`)
          : `${tr('保存失败', 'Couldn’t save')}：${e instanceof Error ? e.message : String(e)}`,
        'error',
      );
    },
  });

  if (isLoading || isError) {
    return (
      <SettingsStack>
        <SettingsSection title={tr('实验环境', 'Experiment environment')}>
          <SettingsGroup>
            {isLoading ? (
              <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
            ) : (
              <SettingsRow label={tr('无法加载实验设置', 'Couldn’t load experiment settings')}>
                <button className="btn btn-ghost sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
              </SettingsRow>
            )}
          </SettingsGroup>
        </SettingsSection>
        <ManagedCommandWatchdogAdminCard />
      </SettingsStack>
    );
  }

  const renderField = (f: TextField) => (
    <SettingsRow
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
    </SettingsRow>
  );

  const setEnvVars = (env_vars: ExperimentEnvVar[]) => {
    setBadField(null);
    setDraft({ ...shown, env_vars });
  };
  const pythonCount = pythonConfiguredCount(saved);

  return (
    <SettingsStack>
    <SettingsSection
      title={tr('实验环境', 'Experiment environment')}
      desc={tr('告诉实验数据和软件在服务器上的位置，不需要的项留空。', 'Tell experiments where data and software live on the server. Leave unused fields empty.')}
    >
      <SettingsGroup>
      {GENERAL_FIELDS.map(renderField)}

      <SettingsRow
        stack
        label={tr('自定义环境变量', 'Custom environment variables')}
        hint={tr('每次实验启动时设置，例如软件安装位置或许可证服务器', 'Set when each experiment starts, e.g. where software is installed')}
        error={badField === 'env_vars' ? tr('有变量名或值不正确', 'A variable name or value isn’t valid') : null}
      >
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
                  placeholder={tr('值', 'Value')}
                  spellCheck={false}
                  autoComplete="off"
                  aria-label={tr('变量值', 'Variable value')}
                  onChange={(e) => setEnvVars(shown.env_vars.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                />
                <button
                  className="btn btn-ghost sm"
                  title={tr('删除', 'Remove')}
                  onClick={() => setEnvVars(shown.env_vars.filter((_, j) => j !== i))}
                >
                  <Icon name="x" size={12} />
                </button>
              </div>
              {envIssues[i] && <div className="field-error">{envIssues[i]}</div>}
            </div>
          ))}
          <div>
            <button className="btn btn-ghost sm" onClick={() => setEnvVars([...shown.env_vars, { name: '', value: '' }])}>
              <Icon name="plus" size={12} />
              {tr('添加变量', 'Add variable')}
            </button>
          </div>
        </div>
      </SettingsRow>
      </SettingsGroup>

      {/* 配过才默认展开：key 随「配没配」变化，保存后按新状态重新决定开合 */}
      <details key={pythonCount > 0 ? 'open' : 'closed'} open={pythonCount > 0} className="st-disclosure">
        <summary className="st-section-label">
          {tr('Python / 机器学习', 'Python / machine learning')}
          <span style={{ marginLeft: 8, fontWeight: 400 }}>
            {pythonCount > 0
              ? tr(`已设置 ${pythonCount} 项`, `${pythonCount} set`)
              : tr('模型目录、pip 镜像、HuggingFace 地址', 'Model directory, pip index, HuggingFace endpoint')}
          </span>
        </summary>
        <SettingsGroup>{PYTHON_FIELDS.map(renderField)}</SettingsGroup>
      </details>

      <SettingsActions>
        <button
          className="btn btn-primary sm"
          disabled={!dirty || hasEnvIssue || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </SettingsActions>
    </SettingsSection>
    <ManagedCommandWatchdogAdminCard />
    </SettingsStack>
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
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(
      `${tr('保存失败', 'Couldn’t save')}：${error instanceof Error ? error.message : String(error)}`,
      'error',
    ),
  });
  return (
    <SettingsSection title={tr('远程命令超时', 'Remote command timeout')}>
      <SettingsGroup>
        <SettingsRow
          label={tr('最长等待（分钟）', 'Longest wait (minutes)')}
          hint={query.isError
            ? tr('无法加载此设置', 'Couldn’t load this setting')
            : tr('到时终止仍占用 GPU 的命令；15–10080，默认 120', 'Then stops commands still using a GPU. 15–10,080, default 120.')}
        >
          <input
            className="input mono st-num"
            type="number"
            min={15}
            max={10080}
            value={shown}
            disabled={query.isError}
            onChange={(event) => setMinutes(Number(event.target.value))}
          />
          <button
            className="btn btn-primary sm"
            disabled={query.isLoading || shown < 15 || shown > 10080 || mutation.isPending || shown === query.data?.max_unanswered_minutes}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
          </button>
        </SettingsRow>
      </SettingsGroup>
    </SettingsSection>
  );
}
