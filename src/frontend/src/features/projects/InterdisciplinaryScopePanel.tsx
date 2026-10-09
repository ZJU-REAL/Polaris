import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type DirectionLibrarySummary,
  type InterdisciplinaryScopeDraft,
  type InterdisciplinaryScopeRead,
  type ProjectRead,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { errorText } from '../../lib/errors';
import {
  splitInterdisciplinaryTerms,
  validateInterdisciplinaryScope,
} from './interdisciplinaryWorkflow';
import './interdisciplinary.css';

interface EditableScope {
  researchScope: string;
  coreQuestions: string;
  primaryDomain: string;
  relatedDomains: string;
  evidenceBoundary: string;
  validationConditions: string;
  userQuestions: Record<string, unknown>[] | null;
  queryMatrix: Record<string, unknown>[] | null;
  evidenceBalance: Record<string, number> | null;
}

/** 版本状态（draft / confirmed）→ 给人看的词；认不出的原样显示。 */
function scopeStatusLabel(status: string): string {
  if (status === 'draft') return tr('草案', 'Draft');
  if (status === 'confirmed') return tr('已确认', 'Confirmed');
  return status;
}

function editableScope(scope: InterdisciplinaryScopeDraft): EditableScope {
  return {
    researchScope: scope.research_scope,
    coreQuestions: scope.core_questions.join('\n'),
    primaryDomain: scope.primary_domain,
    relatedDomains: scope.related_domains.join(', '),
    evidenceBoundary: scope.evidence_boundary ?? '',
    validationConditions: (scope.validation_conditions ?? []).join('\n'),
    userQuestions: scope.user_questions ?? null,
    queryMatrix: scope.query_matrix ?? null,
    evidenceBalance: scope.evidence_balance ?? null,
  };
}

function scopeDraft(scope: EditableScope): InterdisciplinaryScopeDraft {
  return {
    research_scope: scope.researchScope.trim(),
    core_questions: scope.coreQuestions
      .split(/\n+/)
      .map((item) => item.trim())
      .filter(Boolean),
    primary_domain: scope.primaryDomain.trim(),
    related_domains: splitInterdisciplinaryTerms(scope.relatedDomains),
    evidence_boundary: scope.evidenceBoundary.trim() || null,
    validation_conditions: scope.validationConditions
      .split(/\n+/)
      .map((item) => item.trim())
      .filter(Boolean),
    user_questions: scope.userQuestions,
    query_matrix: scope.queryMatrix,
    evidence_balance: scope.evidenceBalance,
  };
}

export function InterdisciplinaryScopePanel({
  project,
  dedicatedLibrary,
}: {
  project: ProjectRead;
  dedicatedLibrary?: DirectionLibrarySummary;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<EditableScope | null>(null);
  const [context, setContext] = useState('');
  const [clarificationQuestions, setClarificationQuestions] = useState<string[]>([]);
  const [rationale, setRationale] = useState('');
  const [model, setModel] = useState('');


  const scopeQuery = useQuery({
    queryKey: ['interdisciplinary-scope', project.id],
    queryFn: () => api.getInterdisciplinaryScope(project.id),
    retry: false,
  });
  const versionsQuery = useQuery({
    queryKey: ['interdisciplinary-scope-versions', project.id],
    queryFn: () => api.listInterdisciplinaryScopeVersions(project.id),
    retry: false,
  });

  useEffect(() => {
    if (scopeQuery.data) setEditing(editableScope(scopeQuery.data));
  }, [scopeQuery.data]);

  const analyze = useMutation({
    mutationFn: () => api.suggestInterdisciplinaryScope({
      name: project.name,
      statement: project.statement ?? project.name,
      ...(context.trim() ? { user_context: context.trim() } : {}),
    }),
    onSuccess: (suggestion) => {
      setEditing(editableScope(suggestion));
      setClarificationQuestions(suggestion.clarification_questions);
      setRationale(suggestion.rationale);
      setModel(suggestion.model);
      toast(tr('已生成新草案', 'New draft ready'), 'ok');
    },
    onError: (error) => toast(
      `${tr('无法分析研究范围：', 'Couldn’t analyze the scope: ')}${errorText(error)}`,
      'error',
    ),
  });

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['interdisciplinary-scope', project.id] });
    void queryClient.invalidateQueries({ queryKey: ['interdisciplinary-scope-versions', project.id] });
    void queryClient.invalidateQueries({ queryKey: ['sourceLibraries', project.id] });
    void queryClient.invalidateQueries({ queryKey: ['libraries'] });
    void queryClient.invalidateQueries({ queryKey: ['projects'] });
  }

  const save = useMutation({
    mutationFn: async (confirm: boolean) => {
      if (!editing) throw new Error('INTERDISCIPLINARY_SCOPE_REQUIRED');
      const draft = scopeDraft(editing);
      const invalid = validateInterdisciplinaryScope(draft);
      if (invalid) throw new Error(`INTERDISCIPLINARY_SCOPE_INVALID:${invalid}`);
      const saved = await api.saveInterdisciplinaryScope(project.id, draft);
      if (!confirm) return { profile: saved, library_id: null };
      return api.confirmInterdisciplinaryScope(project.id);
    },
    onSuccess: (result, confirm) => {
      invalidate();
      if (confirm) {
        toast(tr('研究范围已确认，专属文献库已更新', 'Scope confirmed. The topic library is updated.'), 'ok');
      } else {
        setEditing(editableScope(result.profile));
        toast(tr('草案已保存', 'Draft saved'), 'ok');
      }
    },
    onError: (error) => toast(
      `${tr('无法保存：', 'Couldn’t save: ')}${errorText(error)}`,
      'error',
    ),
  });

  const scopeMissing = scopeQuery.error instanceof ApiError && scopeQuery.error.status === 404;
  const versions = versionsQuery.data ?? [];
  const latest = scopeQuery.data;

  if (scopeQuery.isLoading && !editing) {
    return <section className="card interdisciplinary-profile-card"><div className="skel interdisciplinary-profile-skeleton" /></section>;
  }

  return (
    <section className="card interdisciplinary-profile-card">
      <div className="interdisciplinary-profile-head">
        <div>
          <span className="pill sm">{tr('跨学科研究', 'Interdisciplinary')}</span>
          <h3>{tr('研究范围', 'Research scope')}</h3>
          <p>
            {latest
              ? tr('每次保存生成一个新版本，确认后更新专属文献库。', 'Each save makes a new version. Confirming updates the topic library.')
              : tr('研究范围还没有完成，请重新分析。', 'The scope isn’t finished. Analyze again.')}
          </p>
        </div>
        <div className="row gap8 interdisciplinary-profile-actions">
          {latest && <span className="muted">v{latest.version} · {scopeStatusLabel(latest.status)}</span>}
          {dedicatedLibrary && (
            <button className="btn btn-soft sm" onClick={() => navigate(`/libraries/${dedicatedLibrary.id}`)}>
              <Icon name="book" size={12} />
              {tr('专属文献库', 'Topic library')}
            </button>
          )}
          <button className="btn btn-soft sm" disabled={analyze.isPending || save.isPending} onClick={() => analyze.mutate()}>
            <Icon name={analyze.isPending ? 'refresh' : 'sparkle'} size={12} />
            {analyze.isPending ? tr('分析中…', 'Analyzing…') : tr('重新分析', 'Analyze again')}
          </button>
        </div>
      </div>

      {(scopeMissing || !latest) && !editing && (
        <div className="interdisciplinary-recovery">
          {tr('还没有保存的研究范围，点击「重新分析」生成草案。', 'No saved scope yet. Click “Analyze again” to draft one.')}
        </div>
      )}

      {!!clarificationQuestions.length && (
        <div className="interdisciplinary-questions">
          <span className="label">{tr('需要你回答的问题', 'Questions for you')}</span>
          {clarificationQuestions.map((question, index) => (
            <div key={`${index}-${question}`}><b>{index + 1}</b><span>{question}</span></div>
          ))}
          <textarea className="textarea" rows={3} value={context} onChange={(event) => setContext(event.target.value)} placeholder={tr('在这里回答，然后重新分析', 'Answer here, then analyze again')} />
        </div>
      )}

      {editing && (
        <>
          <div className="interdisciplinary-profile-grid">
            <label className="col gap6 profile-span">
              <span className="label">{tr('研究范围', 'Scope')}</span>
              <textarea className="textarea" rows={4} value={editing.researchScope} onChange={(event) => setEditing({ ...editing, researchScope: event.target.value })} />
            </label>
            <label className="col gap6 profile-span">
              <span className="label">{tr('核心问题', 'Core questions')}</span>
              <textarea className="textarea" rows={4} value={editing.coreQuestions} onChange={(event) => setEditing({ ...editing, coreQuestions: event.target.value })} />
            </label>
            <label className="col gap6">
              <span className="label">{tr('主学科', 'Primary field')}</span>
              <input className="input" value={editing.primaryDomain} onChange={(event) => setEditing({ ...editing, primaryDomain: event.target.value, queryMatrix: null, evidenceBalance: null })} />
            </label>
            <label className="col gap6">
              <span className="label">{tr('相关学科', 'Related fields')}</span>
              <input className="input" value={editing.relatedDomains} onChange={(event) => setEditing({ ...editing, relatedDomains: event.target.value, queryMatrix: null, evidenceBalance: null })} />
            </label>
            <label className="col gap6">
              <span className="label">{tr('证据范围', 'Evidence scope')}</span>
              <textarea className="textarea" rows={3} value={editing.evidenceBoundary} onChange={(event) => setEditing({ ...editing, evidenceBoundary: event.target.value })} />
            </label>
            <label className="col gap6">
              <span className="label">{tr('验证条件', 'Validation criteria')}</span>
              <textarea className="textarea" rows={3} value={editing.validationConditions} onChange={(event) => setEditing({ ...editing, validationConditions: event.target.value })} />
            </label>
          </div>
          {rationale && (
            <div className="interdisciplinary-rationale">
              <span>{tr('依据', 'Rationale')}</span>
              <p>{rationale}</p>
              {model && <small>{model}</small>}
            </div>
          )}
          <div className="row gap8 interdisciplinary-save-actions">
            <button className="btn btn-soft sm" disabled={save.isPending} onClick={() => save.mutate(false)}>
              <Icon name="check" size={12} />
              {save.isPending ? tr('正在保存…', 'Saving…') : tr('保存草案', 'Save draft')}
            </button>
            <button className="btn btn-primary sm" disabled={save.isPending} onClick={() => save.mutate(true)}>
              <Icon name="sparkle" size={12} />
              {tr('确认范围', 'Confirm scope')}
            </button>
          </div>
        </>
      )}

      {!!versions.length && (
        <details className="interdisciplinary-version-history">
          <summary>{tr(`历史版本（${versions.length}）`, `Version history (${versions.length})`)}</summary>
          {versions.map((version: InterdisciplinaryScopeRead) => (
            <div key={version.id}>
              <span className="mono">v{version.version}</span>
              <b>{scopeStatusLabel(version.status)}</b>
              <span>{version.primary_domain} · {version.related_domains.join(' / ')}</span>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
