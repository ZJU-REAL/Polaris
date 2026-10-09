import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import {
  api,
  type InterdisciplinaryScopeDraft,
  type InterdisciplinaryScopeSuggestion,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { errorText } from '../../lib/errors';
import { useLibraries } from '../libraries/hooks';
import { LibraryPicker } from '../libraries/LibraryPicker';
import { ResearchModeFields } from './ResearchModeFields';
import {
  createInterdisciplinaryProject,
  InterdisciplinarySetupError,
  splitInterdisciplinaryTerms,
  validateInterdisciplinaryScope,
  type ResearchMode,
} from './interdisciplinaryWorkflow';

export function ProjectWizardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setCurrentProjectId } = useProject();

  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [researchMode, setResearchMode] = useState<ResearchMode>('conventional');
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [analyzingScope, setAnalyzingScope] = useState(false);
  const [scopeContext, setScopeContext] = useState('');
  const [suggestion, setSuggestion] = useState<InterdisciplinaryScopeSuggestion | null>(null);
  const [researchScope, setResearchScope] = useState('');
  const [coreQuestions, setCoreQuestions] = useState('');
  const [primaryDomain, setPrimaryDomain] = useState('');
  const [relatedDomains, setRelatedDomains] = useState('');
  const [evidenceBoundary, setEvidenceBoundary] = useState('');
  const [validationConditions, setValidationConditions] = useState('');

  const librariesQuery = useLibraries();
  const libraries = librariesQuery.data ?? [];
  const [selectedLibraryIds, setSelectedLibraryIds] = useState<Set<string>>(new Set());

  function toggleLibrary(libraryId: string) {
    setSelectedLibraryIds((previous) => {
      const next = new Set(previous);
      if (next.has(libraryId)) next.delete(libraryId);
      else next.add(libraryId);
      return next;
    });
  }

  function applySuggestion(next: InterdisciplinaryScopeSuggestion) {
    setSuggestion(next);
    setResearchScope(next.research_scope);
    setCoreQuestions(next.core_questions.join('\n'));
    setPrimaryDomain(next.primary_domain);
    setRelatedDomains(next.related_domains.join(', '));
    setEvidenceBoundary(next.evidence_boundary ?? '');
    setValidationConditions((next.validation_conditions ?? []).join('\n'));
  }

  function buildScopeDraft(): InterdisciplinaryScopeDraft {
    return {
      research_scope: researchScope.trim(),
      core_questions: coreQuestions
        .split(/\n+/)
        .map((item) => item.trim())
        .filter(Boolean),
      primary_domain: primaryDomain.trim(),
      related_domains: splitInterdisciplinaryTerms(relatedDomains),
      evidence_boundary: evidenceBoundary.trim() || null,
      validation_conditions: validationConditions
        .split(/\n+/)
        .map((item) => item.trim())
        .filter(Boolean),
      user_questions: suggestion?.user_questions ?? null,
      query_matrix: suggestion?.query_matrix ?? null,
      evidence_balance: suggestion?.evidence_balance ?? null,
    };
  }

  async function analyzeScope() {
    if (!name.trim() || statement.trim().length < 5) {
      setFormError(
        tr(
          '请先填写课题名称和简介（至少 5 个字）',
          'Enter a name and a description of at least 5 characters first.',
        ),
      );
      return;
    }
    setFormError(null);
    setAnalyzingScope(true);
    try {
      const next = await api.suggestInterdisciplinaryScope({
        name: name.trim(),
        statement: statement.trim(),
        ...(scopeContext.trim() ? { user_context: scopeContext.trim() } : {}),
      });
      applySuggestion(next);
      toast(tr('已生成研究范围草案', 'Scope draft ready'), 'ok');
    } catch (error) {
      toast(
        `${tr('无法分析研究范围：', 'Couldn’t analyze the scope: ')}${errorText(error)}`,
        'error',
      );
    } finally {
      setAnalyzingScope(false);
    }
  }

  async function finishCreation(projectId: string) {
    await queryClient.invalidateQueries({ queryKey: ['projects'] });
    await queryClient.invalidateQueries({ queryKey: ['sourceLibraries', projectId] });
    setCurrentProjectId(projectId);
  }

  async function create() {
    if (!name.trim()) {
      setFormError(tr('请填写课题名称', 'Enter a topic name.'));
      return;
    }
    if (researchMode === 'interdisciplinary' && statement.trim().length < 5) {
      setFormError(
        tr(
          '跨学科课题需要填写简介（至少 5 个字）',
          'Interdisciplinary topics need a description of at least 5 characters.',
        ),
      );
      return;
    }

    const scope = buildScopeDraft();
    const invalidField = researchMode === 'interdisciplinary'
      ? validateInterdisciplinaryScope(scope)
      : null;
    if (invalidField) {
      const labels: Record<string, string> = {
        research_scope: tr('研究范围', 'Scope'),
        core_questions: tr('核心问题', 'Core questions'),
        primary_domain: tr('主学科', 'Primary field'),
        related_domains: tr('相关学科', 'Related fields'),
      };
      setFormError(
        tr(
          `请填写「${labels[invalidField] ?? invalidField}」`,
          `Fill in “${labels[invalidField] ?? invalidField}”.`,
        ),
      );
      return;
    }

    setFormError(null);
    setSubmitting(true);
    try {
      if (researchMode === 'conventional') {
        const created = await api.createProject({
          name: name.trim(),
          statement: statement.trim() || undefined,
          source_library_ids: [...selectedLibraryIds],
          research_mode: 'conventional',
        });
        await finishCreation(created.id);
        toast(tr('课题已创建', 'Topic created'), 'ok');
        navigate(topicPath(created.id));
        return;
      }

      const { project } = await createInterdisciplinaryProject(api, {
        name: name.trim(),
        statement: statement.trim(),
        sourceLibraryIds: [...selectedLibraryIds],
        scope,
      });
      await finishCreation(project.id);
      toast(tr('课题和专属文献库已创建', 'Topic and its library created'), 'ok');
      navigate(topicPath(project.id));
    } catch (error) {
      if (error instanceof InterdisciplinarySetupError) {
        await finishCreation(error.project.id);
        const phase = error.stage === 'save-scope'
          ? tr('研究范围没有保存', 'the scope wasn’t saved')
          : tr('研究范围没有确认，专属文献库也没有创建', 'the scope wasn’t confirmed and its library wasn’t created');
        toast(
          `${tr('课题已创建，但', 'The topic was created, but ')}${phase}${tr('，请在课题设置中重试：', '. Try again in topic settings: ')}${errorText(error)}`,
          'error',
        );
        navigate(`${topicPath(error.project.id)}?tab=settings`);
        return;
      }
      toast(
        `${tr('无法创建课题：', 'Couldn’t create the topic: ')}${errorText(error)}`,
        'error',
      );
    } finally {
      setSubmitting(false);
    }
  }

  const scopeReady = researchMode === 'conventional'
    || (suggestion !== null && validateInterdisciplinaryScope(buildScopeDraft()) === null);

  return (
    <div className="page fadeup project-wizard-page">
      <PageHead eyebrow={tr('课题', 'Topics')} title={tr('新建课题', 'New Topic')} />

      <div className="card card-pad project-wizard-card">
        <FormField label={tr('名称', 'Name')}>
          <input
            className="input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={tr('例如 LLM 自主科研智能体', 'e.g. Autonomous research agents')}
          />
        </FormField>
        <FormField
          label={tr('简介', 'Description')}
          hint={researchMode === 'interdisciplinary'
            ? tr('一句话说明研究什么，用于分析研究范围', 'One sentence on what you study. Used to analyze the scope.')
            : tr('一句话说明研究什么（可选）', 'One sentence on what you study (optional)')}
        >
          <textarea
            className="textarea"
            rows={3}
            value={statement}
            onChange={(event) => setStatement(event.target.value)}
            placeholder={tr(
              '例如 用 LLM 智能体自动完成从文献调研到论文写作的全流程',
              'e.g. LLM agents that carry research from literature review to a written paper',
            )}
          />
        </FormField>
        <ResearchModeFields
          mode={researchMode}
          onModeChange={(mode) => {
            setResearchMode(mode);
            setFormError(null);
          }}
        />
        {formError && <div className="field-error project-wizard-error">{formError}</div>}
      </div>

      {researchMode === 'interdisciplinary' && (
        <div className="card card-pad interdisciplinary-scope-wizard">
          <div className="interdisciplinary-scope-head">
            <div>
              <span className="section-h">
                <Icon name="layers" size={15} />
                {tr('研究范围', 'Research scope')}
              </span>
              <p>
                {tr(
                  '先生成学科边界和核心问题，再由你修改确认。',
                  'Generate the fields and core questions, then edit and confirm them.',
                )}
              </p>
            </div>
            <button className="btn btn-soft" disabled={analyzingScope || submitting} onClick={() => void analyzeScope()}>
              <Icon name={analyzingScope ? 'refresh' : 'sparkle'} size={14} />
              {analyzingScope
                ? tr('分析中…', 'Analyzing…')
                : suggestion
                  ? tr('重新分析', 'Analyze again')
                  : tr('分析范围', 'Analyze scope')}
            </button>
          </div>

          {!!suggestion?.clarification_questions.length && (
            <div className="interdisciplinary-questions">
              <span className="label">{tr('需要你回答的问题', 'Questions for you')}</span>
              {suggestion.clarification_questions.map((question, index) => (
                <div key={`${index}-${question}`}>
                  <b>{index + 1}</b>
                  <span>{question}</span>
                </div>
              ))}
              <textarea
                className="textarea"
                rows={3}
                value={scopeContext}
                onChange={(event) => setScopeContext(event.target.value)}
                placeholder={tr('在这里回答，然后重新分析', 'Answer here, then analyze again')}
              />
            </div>
          )}

          {suggestion && (
            <div className="interdisciplinary-scope-review">
              <FormField label={tr('研究范围', 'Scope')}>
                <textarea className="textarea" rows={4} value={researchScope} onChange={(event) => setResearchScope(event.target.value)} />
              </FormField>
              <FormField label={tr('核心问题', 'Core questions')} hint={tr('每行一个', 'One per line')}>
                <textarea className="textarea" rows={4} value={coreQuestions} onChange={(event) => setCoreQuestions(event.target.value)} />
              </FormField>
              <div className="interdisciplinary-field-grid">
                <FormField label={tr('主学科', 'Primary field')}>
                  <input className="input" value={primaryDomain} onChange={(event) => setPrimaryDomain(event.target.value)} />
                </FormField>
                <FormField label={tr('相关学科', 'Related fields')} hint={tr('用逗号分隔', 'Separate with commas')}>
                  <input className="input" value={relatedDomains} onChange={(event) => setRelatedDomains(event.target.value)} />
                </FormField>
              </div>
              <div className="interdisciplinary-field-grid">
                <FormField label={tr('证据范围', 'Evidence scope')}>
                  <textarea className="textarea" rows={3} value={evidenceBoundary} onChange={(event) => setEvidenceBoundary(event.target.value)} />
                </FormField>
                <FormField label={tr('验证条件', 'Validation criteria')} hint={tr('每行一个', 'One per line')}>
                  <textarea className="textarea" rows={3} value={validationConditions} onChange={(event) => setValidationConditions(event.target.value)} />
                </FormField>
              </div>
              <div className="interdisciplinary-rationale">
                <span>{tr('依据', 'Rationale')}</span>
                <p>{suggestion.rationale}</p>
                <small>{suggestion.model}</small>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="card card-pad project-wizard-card">
        <div className="row project-wizard-section-head">
          <span className="section-h">
            <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
            {tr('关联文献库', 'Linked libraries')}
          </span>
          {libraries.length > 0 && (
            <span className="muted project-wizard-selection-count">
              {tr(`已选 ${selectedLibraryIds.size} 个`, `${selectedLibraryIds.size} selected`)}
            </span>
          )}
        </div>
        <p className="project-wizard-hint">
          {researchMode === 'interdisciplinary'
            ? tr(
                '可选。创建时还会新建一个本课题专属的文献库。',
                'Optional. A library for this topic is also created.',
              )
            : tr('可选，之后也能在课题设置中添加。', 'Optional. You can add them later in topic settings.')}
        </p>
        {librariesQuery.isLoading ? (
          <div className="col gap8">
            {[0, 1].map((index) => <div key={index} className="skel project-wizard-library-skeleton" />)}
          </div>
        ) : libraries.length === 0 ? (
          <div className="empty project-wizard-empty">
            <div>{tr('还没有文献库', 'No libraries yet')}</div>
            <button className="btn btn-soft sm" onClick={() => navigate('/libraries')}>
              <Icon name="book" size={13} />
              {tr('新建文献库', 'New library')}
            </button>
          </div>
        ) : (
          <LibraryPicker libraries={libraries} selectedIds={selectedLibraryIds} onToggle={toggleLibrary} />
        )}
      </div>

      <div className="row project-wizard-actions">
        <button
          className="btn btn-primary"
          onClick={() => void create()}
          disabled={submitting || analyzingScope || !scopeReady}
        >
          <Icon name="check" size={14} />
          {submitting
            ? tr('创建中…', 'Creating…')
            : researchMode === 'interdisciplinary'
              ? tr('确认范围并创建', 'Confirm scope and create')
              : tr('创建课题', 'Create topic')}
        </button>
      </div>
    </div>
  );
}
