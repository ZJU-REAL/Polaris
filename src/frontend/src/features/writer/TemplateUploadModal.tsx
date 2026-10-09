import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { api, ApiError, type TemplateInfo } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   上传论文模板Modal：zip + 名称 + 说明 + 引擎 + 页数上限
   → POST /manuscripts/templates（multipart）。
   默认项目私有（传 pid）；勾设为全平台可用则不传 project_id（需平台管理员）。
   成功后把新模板 id 回调给调用方（用于刷新列表并选中）。
   ============================================================ */

const ENGINES = ['tectonic', 'pdflatex', 'xelatex', 'lualatex'] as const;

export interface TemplateUploadModalProps {
  open: boolean;
  onClose: () => void;
  /** 当前研究方向；上传为项目私有模板时传给后端。 */
  pid: string;
  /** 上传成功回调，携带新模板 id（供调用方刷新并选中）。 */
  onUploaded: (template: TemplateInfo) => void;
}

export function TemplateUploadModal({ open, onClose, pid, onUploaded }: TemplateUploadModalProps) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [engine, setEngine] = useState<string>('tectonic');
  const [pageLimit, setPageLimit] = useState('');
  const [global, setGlobal] = useState(false);

  useEffect(() => {
    if (!open) return;
    setFile(null);
    setName('');
    setDescription('');
    setEngine('tectonic');
    setPageLimit('');
    setGlobal(false);
  }, [open]);

  const mutation = useMutation({
    mutationFn: () =>
      api.uploadManuscriptTemplate({
        file: file!,
        name: name.trim(),
        ...(description.trim() ? { description: description.trim() } : {}),
        engine,
        ...(pageLimit.trim() ? { page_limit: Number(pageLimit) } : {}),
        ...(global ? {} : { project_id: pid }),
      }),
    onSuccess: (tpl) => {
      toast(tr('已上传模板', 'Template uploaded'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript-templates'] });
      onUploaded(tpl);
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError) {
        if (e.status === 422) {
          toast(tr('zip 中没有 .tex 文件，请检查模板包', 'The zip has no .tex file. Check the template package.'), 'error');
          return;
        }
      }
      toast(`${tr('上传失败：', 'Couldn’t upload: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    },
  });

  const canSubmit = !!file && !!name.trim() && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={520}
      title={
        <>
          <Icon name="file" size={16} style={{ color: 'var(--accent)' }} />
          {tr('上传模板', 'Upload template')}
        </>
      }
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('上传中…', 'Uploading…')}
              </>
            ) : (
              <>
                <Icon name="plus" size={14} />
                {tr('上传', 'Upload')}
              </>
            )}
          </button>
        </>
      }
    >
      <FormField
        label={tr('模板包', 'Template package')}
        hint={tr('zip 格式，至少包含一个 .tex 文件', 'A zip with at least one .tex file')}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".zip,application/zip"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0] ?? null;
            setFile(f);
            if (f && !name.trim()) setName(f.name.replace(/\.zip$/i, ''));
            e.target.value = '';
          }}
        />
        <div className="row gap8" style={{ alignItems: 'center' }}>
          <button className="btn btn-soft" onClick={() => fileInputRef.current?.click()}>
            <Icon name="file" size={14} />
            {file ? tr('重新选择', 'Choose another') : tr('选择 zip', 'Choose zip')}
          </button>
          <span style={{ fontSize: 12, color: file ? 'var(--text-2)' : 'var(--text-3)' }}>
            {file ? file.name : tr('未选择文件', 'No file selected')}
          </span>
        </div>
      </FormField>

      <FormField label={tr('名称', 'Name')}>
        <input
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={tr('例如：内部技术报告', 'e.g. Internal tech report')}
        />
      </FormField>

      <FormField label={tr('说明（可选）', 'Description (optional)')}>
        <textarea
          className="textarea"
          rows={2}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={tr('例如：组内技术报告格式', 'e.g. Lab tech report format')}
        />
      </FormField>

      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField label={tr('编译引擎', 'Engine')} style={{ flex: 1 }}>
          <SelectMenu
            value={engine}
            options={ENGINES.map((en) => ({ value: en, label: en }))}
            onChange={setEngine}
          />
        </FormField>
        <FormField
          label={tr('页数上限（可选）', 'Page limit (optional)')}
          style={{ flex: 1 }}
        >
          <input
            className="input"
            type="number"
            min={1}
            value={pageLimit}
            onChange={(e) => setPageLimit(e.target.value)}
            placeholder={tr('例如 9', 'e.g. 9')}
          />
        </FormField>
      </div>

      <label className="row gap8" style={{ fontSize: 13, cursor: 'pointer', marginTop: 4 }}>
        <input type="checkbox" checked={global} onChange={(e) => setGlobal(e.target.checked)} />
        {tr('在所有课题中可用', 'Use in all topics')}
      </label>
      <div className="field-hint" style={{ marginTop: 4 }}>
        {tr('不勾选则只在当前课题可用', 'Otherwise only this topic can use it')}
      </div>
    </Modal>
  );
}
