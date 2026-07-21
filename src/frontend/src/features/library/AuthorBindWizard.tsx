import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, type AuthorProfile } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   署名信息表单（PublicationsTab 与设置页「学术身份」共用）：
   多个姓名写法 + 多个机构，保存即完成；
   之后每天自动从文献库匹配你发表的论文。
   ============================================================ */

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 小巧的多值输入：已有值显示为可删除的 chip，输入后回车 / 逗号 / 失焦追加。 */
export function MultiValueInput({
  values,
  onChange,
  placeholder,
  autoFocus,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  const [draft, setDraft] = useState('');

  const commit = () => {
    const v = draft.trim();
    setDraft('');
    if (!v) return;
    if (values.some((x) => x.toLowerCase() === v.toLowerCase())) return;
    onChange([...values, v]);
  };

  return (
    <div
      className="input"
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 5,
        height: 'auto',
        minHeight: 32,
        padding: '4px 8px',
        cursor: 'text',
      }}
    >
      {values.map((v) => (
        <span
          key={v}
          className="tag"
          style={{ display: 'inline-flex', alignItems: 'center', gap: 4, maxWidth: '100%' }}
        >
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</span>
          <span
            title={tr('移除', 'Remove')}
            style={{ cursor: 'pointer', display: 'inline-flex', opacity: 0.6, flexShrink: 0 }}
            onClick={() => onChange(values.filter((x) => x !== v))}
          >
            <Icon name="x" size={9} />
          </span>
        </span>
      ))}
      <input
        value={draft}
        autoFocus={autoFocus}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if ((e.key === 'Enter' || e.key === ',') && !e.nativeEvent.isComposing) {
            e.preventDefault();
            commit();
          }
          if (e.key === 'Backspace' && !draft && values.length > 0) {
            onChange(values.slice(0, -1));
          }
        }}
        placeholder={values.length === 0 ? placeholder : undefined}
        style={{
          flex: 1,
          minWidth: 90,
          border: 'none',
          outline: 'none',
          background: 'transparent',
          font: 'inherit',
          fontSize: 12.5,
          color: 'var(--text)',
          padding: '2px 0',
        }}
      />
    </div>
  );
}

export function AuthorBindWizard({
  profile,
  onDone,
  onCancel,
  embedded = false,
}: {
  /** 已有绑定时用于预填（修改绑定入口）；未绑定时为 null。 */
  profile: AuthorProfile | null;
  onDone: () => void;
  onCancel?: () => void;
  /** 内嵌进别的卡片时不再套自己的卡片外壳。 */
  embedded?: boolean;
}) {
  const queryClient = useQueryClient();
  const [names, setNames] = useState<string[]>(profile?.name_variants ?? []);
  const [affiliations, setAffiliations] = useState<string[]>(profile?.affiliations ?? []);

  const saveMutation = useMutation({
    mutationFn: () =>
      api.saveAuthorProfile({
        name_variants: names,
        affiliations,
        openalex_author_id: null,
        orcid: profile?.orcid ?? null,
        auto_sync: profile?.auto_sync ?? true,
      }),
    onSuccess: (saved) => {
      queryClient.setQueryData(['author-profile'], saved);
      void queryClient.invalidateQueries({ queryKey: ['author-profile'] });
      void queryClient.invalidateQueries({ queryKey: ['publications'] });
      toast(tr('署名信息已保存', 'Author info saved'), 'ok');
      onDone();
    },
    onError: (e) => toast(`${tr('保存失败：', 'Save failed: ')}${errText(e)}`, 'error'),
  });

  const body = (
    <>
      <div className="section-h">
        <Icon name="users" size={15} />
        {tr('你的署名信息', 'Your author info')}
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--text-2)', margin: '8px 0 16px', lineHeight: 1.6 }}>
        {tr(
          '填写论文署名可能用到的姓名写法和机构，保存后每天会自动从文献库里匹配你发表的论文。',
          'List the name spellings and affiliations you publish under. Once saved, we match your publications from the library daily.',
        )}
      </p>

      <FormField
        label="姓名写法"
        en="Name variants"
        hint={tr('可以填多个，如 San Zhang、Zhang San；回车添加', 'Add as many as you use, e.g. San Zhang, Zhang San — Enter to add')}
      >
        <MultiValueInput values={names} onChange={setNames} placeholder="e.g. San Zhang" autoFocus />
      </FormField>
      <FormField
        label="机构（选填）"
        en="Affiliations (optional)"
        style={{ marginTop: 12 }}
        hint={tr('回车添加，可以填多个', 'Enter to add; multiple allowed')}
      >
        <MultiValueInput values={affiliations} onChange={setAffiliations} placeholder="e.g. Zhejiang University" />
      </FormField>

      <div className="row gap8" style={{ marginTop: 14 }}>
        <button
          className="btn btn-primary sm"
          disabled={names.length === 0 || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          <Icon name="check" size={13} />
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
        {onCancel && (
          <button type="button" className="btn btn-ghost sm" onClick={onCancel}>
            {tr('取消', 'Cancel')}
          </button>
        )}
      </div>
    </>
  );

  if (embedded) {
    return (
      <div style={{ marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--border)' }}>{body}</div>
    );
  }
  return (
    <div className="card card-pad" style={{ maxWidth: 680 }}>
      {body}
    </div>
  );
}
