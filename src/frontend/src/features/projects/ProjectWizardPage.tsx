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
          '跨学科研究需要先填写课题名称和不少于 5 个字符的一句话定义',
          'Enter a topic name and a definition of at least 5 characters first',
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
      toast(tr('已生成可编辑的交叉范围草案', 'Editable scope draft generated'), 'ok');
    } catch (error) {
      toast(
        `${tr('范围分析失败：', 'Scope analysis failed: ')}${error instanceof Error ? error.message : String(error)}`,
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
      setFormError(tr('请填写课题名称', 'Please enter a topic name'));
      return;
    }
    if (researchMode === 'interdisciplinary' && statement.trim().length < 5) {
      setFormError(
        tr(
          '跨学科研究需要填写不少于 5 个字符的一句话定义',
          'Interdisciplinary research needs a definition of at least 5 characters',
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
        research_scope: tr('交叉研究范围', 'research scope'),
        core_questions: tr('核心交叉问题', 'core questions'),
        primary_domain: tr('主学科', 'primary domain'),
        related_domains: tr('关联学科', 'related domains'),
      };
      setFormError(
        tr(
          `请先分析并完整填写${labels[invalidField] ?? invalidField}`,
          `Analyze and complete ${labels[invalidField] ?? invalidField} first`,
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
      toast(tr('跨学科课题和专属交叉文献库已创建', 'Topic and dedicated evidence library created'), 'ok');
      navigate(topicPath(project.id));
    } catch (error) {
      if (error instanceof InterdisciplinarySetupError) {
        await finishCreation(error.project.id);
        const phase = error.stage === 'save-scope'
          ? tr('交叉范围尚未保存', 'the scope was not saved')
          : tr('交叉范围尚未确认，专属库尚未创建', 'the scope was not confirmed and the dedicated library was not created');
        toast(
          `${tr('课题已创建，但', 'The topic was created, but ')}${phase}：${error.message}`,
          'error',
        );
        navigate(`${topicPath(error.project.id)}?tab=settings`);
        return;
      }
      toast(
        `${tr('创建失败：', 'Create failed: ')}${error instanceof Error ? error.message : String(error)}`,
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
      <PageHead eyebrow="Polaris · Topics" title={tr('新建课题', 'New topic')} />

      <ResearchModeFields
        mode={researchMode}
        onModeChange={(mode) => {
          setResearchMode(mode);
          setFormError(null);
        }}
      />

      <div className="card card-pad project-wizard-card">
        <FormField label={tr('课题名称', 'Name')}>
          <input
            className="input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={tr('如：LLM 自主科研智能体', 'e.g. LLM autonomous research agents')}
          />
        </FormField>
        <FormField
          label={tr('一句话定义', 'Statement')}
          hint={researchMode === 'interdisciplinary'
            ? tr('用于分析研究对象、方法来源和验证边界', 'Used to analyze the object, methods and validation boundary')
            : tr('一句话说清研究什么（可选）', 'One sentence on what this topic studies (optional)')}
        >
          <textarea
            className="textarea"
            rows={3}
            value={statement}
            onChange={(event) => setStatement(event.target.value)}
            placeholder={tr(
              '如：让 LLM agent 端到端完成从文献调研到论文的研究方法与系统',
              'e.g. Methods and systems for LLM agents to go end-to-end from literature survey to paper',
            )}
          />
        </FormField>
        {formError && <div className="field-error project-wizard-error">{formError}</div>}
      </div>

      {researchMode === 'interdisciplinary' && (
        <div className="card card-pad interdisciplinary-scope-wizard">
          <div className="interdisciplinary-scope-head">
            <div>
              <span className="section-h">
                <Icon name="layers" size={15} />
                {tr('交叉研究范围', 'Interdisciplinary scope')}
              </span>
              <p>
                {tr(
                  '先让 AI 给出学科边界和核心问题，再由你判断、补充和确认。',
                  'Let AI propose the domain boundary and core questions, then review and confirm them.',
                )}
              </p>
            </div>
            <button className="btn btn-soft" disabled={analyzingScope || submitting} onClick={() => void analyzeScope()}>
              <Icon name={analyzingScope ? 'refresh' : 'sparkle'} size={14} />
              {analyzingScope
                ? tr('分析中…', 'Analyzing…')
                : suggestion
                  ? tr('重新分析', 'Analyze again')
                  : tr('分析交叉范围', 'Analyze scope')}
            </button>
          </div>

          {!!suggestion?.clarification_questions.length && (
            <div className="interdisciplinary-questions">
              <span className="label">{tr('需要你判断的问题', 'Questions for you')}</span>
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
                placeholder={tr('按需补充回答，再点击重新分析', 'Add answers as needed, then analyze again')}
              />
            </div>
          )}

          {suggestion && (
            <div className="interdisciplinary-scope-review">
              <FormField label={tr('交叉研究范围', 'Scope')}>
                <textarea className="textarea" rows={4} value={researchScope} onChange={(event) => setResearchScope(event.target.value)} />
              </FormField>
              <FormField label={tr('核心交叉问题', 'Core questions')} hint={tr('每行一个问题', 'One question per line')}>
                <textarea className="textarea" rows={4} value={coreQuestions} onChange={(event) => setCoreQuestions(event.target.value)} />
              </FormField>
              <div className="interdisciplinary-field-grid">
                <FormField label={tr('主学科', 'Primary domain')}>
                  <input className="input" value={primaryDomain} onChange={(event) => setPrimaryDomain(event.target.value)} />
                </FormField>
                <FormField label={tr('关联学科', 'Related domains')} hint={tr('使用逗号分隔', 'Comma separated')}>
                  <input className="input" value={relatedDomains} onChange={(event) => setRelatedDomains(event.target.value)} />
                </FormField>
              </div>
              <div className="interdisciplinary-field-grid">
                <FormField label={tr('证据边界', 'Evidence boundary')}>
                  <textarea className="textarea" rows={3} value={evidenceBoundary} onChange={(event) => setEvidenceBoundary(event.target.value)} />
                </FormField>
                <FormField label={tr('验证条件', 'Validation conditions')} hint={tr('每行一项', 'One per line')}>
                  <textarea className="textarea" rows={3} value={validationConditions} onChange={(event) => setValidationConditions(event.target.value)} />
                </FormField>
              </div>
              <div className="interdisciplinary-rationale">
                <span>{tr('建议依据', 'Rationale')}</span>
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
                '可关联已有学科库作为补充证据；确认范围后还会自动创建专属交叉文献库。',
                'Optionally link discipline libraries as supporting evidence. A dedicated interdisciplinary library is created after confirmation.',
              )
            : tr('可以不选，稍后在课题设置里添加。', 'Optional. You can add libraries later in topic settings.')}
        </p>
        {librariesQuery.isLoading ? (
          <div className="col gap8">
            {[0, 1].map((index) => <div key={index} className="skel project-wizard-library-skeleton" />)}
          </div>
        ) : libraries.length === 0 ? (
          <div className="empty project-wizard-empty">
            <div>{tr('还没有可关联的文献库。', 'No libraries available to link yet.')}</div>
            <button className="btn btn-soft sm" onClick={() => navigate('/libraries')}>
              <Icon name="book" size={13} />
              {tr('去文献库新建一个（需管理员审批）', 'Create one under Libraries (needs admin approval)')}
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
              ? tr('确认范围并创建课题', 'Confirm scope and create topic')
              : tr('创建课题', 'Create topic')}
        </button>
      </div>
    </div>
  );
}
