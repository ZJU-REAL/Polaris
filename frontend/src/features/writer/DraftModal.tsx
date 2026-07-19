import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type ManuscriptDetail, type ManuscriptFileRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { DEFAULT_SECTIONS, sectionText } from './shared';

/* ============================================================
   「AI 起草」Modal：全部节 / 选节 checkbox + 备注 →
   POST /manuscripts/{id}/draft（kind=paper_writing 的 AI 任务）。
   同稿件已有进行中任务时后端 409。
   ============================================================ */

export interface DraftModalProps {
  open: boolean;
  onClose: () => void;
  manuscript: ManuscriptDetail;
  /** 初始化结构成功后回调：把新生成的 draft.tex 交给编辑器打开。 */
  onInitialized?: (file: ManuscriptFileRead) => void;
}

export function DraftModal({ open, onClose, manuscript, onInitialized }: DraftModalProps) {
  const queryClient = useQueryClient();
  const [all, setAll] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState('');
  const [initConfirmOpen, setInitConfirmOpen] = useState(false);

  // 模板 sections 优先（GET templates），拿不到用固定顺序兜底
  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates', manuscript.project_id],
    queryFn: () => api.listManuscriptTemplates(manuscript.project_id),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const sections = useMemo(() => {
    const tpl = templatesQuery.data?.find((t) => t.id === manuscript.template);
    return tpl?.sections && tpl.sections.length > 0 ? tpl.sections : DEFAULT_SECTIONS;
  }, [templatesQuery.data, manuscript.template]);

  useEffect(() => {
    if (!open) return;
    setAll(true);
    setSelected(new Set());
    setNotes('');
    setInitConfirmOpen(false);
  }, [open]);

  // 初始化结构：新建 draft.tex（照抄当前主文件导言区 + 分节骨架），把编译主文件
  // 切到 draft.tex，原主文件不动。成功后交由父组件在编辑器里打开 draft.tex。
  const initMutation = useMutation({
    mutationFn: () => api.initializeManuscriptStructure(manuscript.id),
    onSuccess: (file) => {
      toast(tr('已生成结构化的 draft.tex 并切换为编译主文件，原文件保留', 'Created a structured draft.tex and switched compile to it — your original file is kept'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript-file'] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript-file-raw'] });
      void queryClient.invalidateQueries({ queryKey: ['file-versions', manuscript.id] });
      setInitConfirmOpen(false);
      onClose();
      // 父组件负责刷新详情并在编辑器里打开 draft.tex
      onInitialized?.(file);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422 && e.message.includes('MAIN_TEX_NO_DOCUMENT')) {
        toast(
          tr(
            '主文件里没有 \\begin{document}…\\end{document}，请先选择正确的主文件',
            'The main file has no \\begin{document}…\\end{document} — pick the correct main file first',
          ),
          'error',
        );
      } else {
        toast(`${tr('初始化失败：', 'Initialize failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const mutation = useMutation({
    mutationFn: () =>
      api.draftManuscript(manuscript.id, {
        sections: all ? null : Array.from(selected),
        ...(notes.trim() ? { notes: notes.trim() } : {}),
      }),
    onSuccess: () => {
      toast(tr('AI 起草任务已启动，可在顶栏查看进度', 'AI drafting started — check progress in the top bar'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscript.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('这篇稿子已经有一个进行中的 AI 起草任务了', 'This manuscript already has a drafting task in progress'), 'error');
      } else {
        toast(`${tr('启动失败：', 'Failed to start: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const canSubmit = !mutation.isPending && (all || selected.size > 0);

  function toggleSection(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <>
    <Modal
      open={open}
      onClose={onClose}
      width={540}
      title={
        <>
          <Icon name="sparkle" size={16} style={{ color: 'var(--accent)' }} />
          {tr('AI 起草', 'AI draft')}
        </>
      }
      sub={tr(
        'AI 按事实包写作：引用、图表、数字都只能来自事实包，逐节自检后经协同编辑实时写入。',
        'AI writes from the fact pack: citations, figures and numbers may only come from it, each section is self-checked and written in via live collaborative editing.',
      )}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('启动中…', 'Starting…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('开始起草', 'Start drafting')}
              </>
            )}
          </button>
        </>
      }
    >
      <div
        style={{
          border: '0.5px solid var(--border)',
          borderRadius: 8,
          padding: '10px 12px',
          background: 'var(--surface-2)',
          marginBottom: 14,
        }}
      >
        <div className="row gap8" style={{ marginBottom: 6 }}>
          <span style={{ fontSize: 12.5, fontWeight: 650 }}>
            {tr('第一步 · 初始化结构', 'Step 1 · Initialize structure')}
          </span>
          <button
            className="btn btn-ghost sm"
            style={{ marginLeft: 'auto' }}
            disabled={initMutation.isPending}
            onClick={() => setInitConfirmOpen(true)}
          >
            {initMutation.isPending ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('初始化中…', 'Initializing…')}
              </>
            ) : (
              <>
                <Icon name="layers" size={13} />
                {tr('初始化结构', 'Initialize structure')}
              </>
            )}
          </button>
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
          {tr(
            '新建一个 draft.tex：照抄当前主文件的导言区，正文换成分节骨架，供 AI 逐节起草；同时把编译主文件切换成 draft.tex。原主文件保持不变。首次起草前做一次即可。',
            'Creates a new draft.tex: copies the current main file’s preamble and replaces the body with a section skeleton for the AI to draft into, then switches the compile main file to draft.tex. Your original main file is left untouched. Do this once before the first draft.',
          )}
        </div>
      </div>

      <FormField label={tr('写哪些节', 'Sections to write')}>
        <div className="col gap8">
          <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
            <input type="radio" checked={all} onChange={() => setAll(true)} />
            {tr(
              '全部节（引言 → 方法 → 实验设置 → 结果 → 结论 → 摘要 → 相关工作，最后整体编译）',
              'All sections (intro → method → setup → results → conclusion → abstract → related work, then a full compile)',
            )}
          </label>
          <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
            <input type="radio" checked={!all} onChange={() => setAll(false)} />
            {tr('只写选中的节', 'Only the selected sections')}
          </label>
          {!all && (
            <div className="row gap8 wrap" style={{ paddingLeft: 22 }}>
              {sections.map((s) => (
                <label key={s} className={`chip${selected.has(s) ? ' on' : ''}`} style={{ gap: 5 }}>
                  <input
                    type="checkbox"
                    checked={selected.has(s)}
                    onChange={() => toggleSection(s)}
                    style={{ display: 'none' }}
                  />
                  {sectionText(s)}
                </label>
              ))}
            </div>
          )}
        </div>
      </FormField>

      <FormField
        label={tr('备注（可选）', 'Notes (optional)')}
        hint={tr('给 AI 的额外要求，比如强调某个贡献点、写作口吻、篇幅取舍。', 'Extra instructions for the AI, e.g. which contribution to highlight, tone, or length trade-offs.')}
      >
        <textarea
          className="textarea"
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder={tr(
            '如：重点突出方法的高效性，Related Work 里对比 XX 一系工作…',
            'e.g. emphasize efficiency; compare against the XX line of work in Related Work…',
          )}
        />
      </FormField>

      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        {tr(
          '起草时编辑器里会出现一个「✨ AI」光标，逐字把每一节写进正文、自动滚动跟随，你可以在旁边实时看着它写（也能随时在 AI 任务页取消）。每一节写完都会做真实性自检：引用必须在事实包文献里、图表只能用实验产出、正文数字必须能对上实验指标。',
          'While drafting, a "✨ AI" cursor types each section into the manuscript character by character and auto-scrolls to follow — watch it write live (or cancel from the AI tasks page). Every finished section is fact-checked: citations must be in the fact pack, figures must come from experiment outputs, and numbers must match experiment metrics.',
        )}
      </div>
    </Modal>

    <ConfirmModal
      open={initConfirmOpen}
      onClose={() => setInitConfirmOpen(false)}
      title={tr('生成 draft.tex', 'Create draft.tex')}
      message={tr(
        '会新建一个 draft.tex（导言区照抄当前主文件，正文是分节骨架），并把编译主文件切换成它，用于分节 AI 起草。原文件不会被改动。继续？',
        'This creates a new draft.tex (preamble copied from the current main file, body as a section skeleton) and switches the compile main file to it for section-by-section AI drafting. Your original file is left untouched. Continue?',
      )}
      confirmText={tr('生成 draft.tex', 'Create draft.tex')}
      busy={initMutation.isPending}
      onConfirm={() => initMutation.mutate()}
    />
    </>
  );
}
