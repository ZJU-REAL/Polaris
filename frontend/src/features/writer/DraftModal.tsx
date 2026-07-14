import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type ManuscriptDetail } from '../../lib/api';
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
}

export function DraftModal({ open, onClose, manuscript }: DraftModalProps) {
  const queryClient = useQueryClient();
  const [all, setAll] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState('');

  // 模板 sections 优先（GET templates），拿不到用固定顺序兜底
  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates'],
    queryFn: () => api.listManuscriptTemplates(),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const sections = useMemo(() => {
    const tpl = templatesQuery.data?.find((t) => t.key === manuscript.template);
    return tpl?.sections && tpl.sections.length > 0 ? tpl.sections : DEFAULT_SECTIONS;
  }, [templatesQuery.data, manuscript.template]);

  useEffect(() => {
    if (!open) return;
    setAll(true);
    setSelected(new Set());
    setNotes('');
  }, [open]);

  const mutation = useMutation({
    mutationFn: () =>
      api.draftManuscript(manuscript.id, {
        sections: all ? null : Array.from(selected),
        ...(notes.trim() ? { notes: notes.trim() } : {}),
      }),
    onSuccess: () => {
      toast('AI 起草任务已启动，可在顶栏查看进度', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscript.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast('这篇稿子已经有一个进行中的 AI 起草任务了', 'error');
      } else {
        toast(`启动失败：${e instanceof Error ? e.message : String(e)}`, 'error');
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
    <Modal
      open={open}
      onClose={onClose}
      width={540}
      title={
        <>
          <Icon name="sparkle" size={16} style={{ color: 'var(--accent)' }} />
          AI 起草
        </>
      }
      sub="AI 按事实包写作：引用、图表、数字都只能来自事实包，逐节自检后经协同编辑实时写入。"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                启动中…
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                开始起草
              </>
            )}
          </button>
        </>
      }
    >
      <FormField label="写哪些节" en="sections">
        <div className="col gap8">
          <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
            <input type="radio" checked={all} onChange={() => setAll(true)} />
            全部节（引言 → 方法 → 实验设置 → 结果 → 结论 → 摘要 → 相关工作，最后整体编译）
          </label>
          <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
            <input type="radio" checked={!all} onChange={() => setAll(false)} />
            只写选中的节
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
        label="备注（可选）"
        en="notes"
        hint="给 AI 的额外要求，比如强调某个贡献点、写作口吻、篇幅取舍。"
      >
        <textarea
          className="textarea"
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="如：重点突出方法的高效性，Related Work 里对比 XX 一系工作…"
        />
      </FormField>

      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        起草期间可以随时打开编辑器围观 AI 写作（协同编辑实时可见），也可以在「AI 任务」页取消任务。
        每一节写完都会做真实性自检：引用必须在事实包文献里、图表只能用实验产出、正文数字必须能对上实验指标。
      </div>
    </Modal>
  );
}
