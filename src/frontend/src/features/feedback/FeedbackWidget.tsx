import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent } from 'react';
import { useLocation } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { toast } from '../../components/ui/Toast';
import { tr, getLang } from '../../lib/i18n';
import { api, type FeedbackSeverity, type FeedbackType } from '../../lib/api';
import { useProject } from '../../app/project';
import { SEVERITIES, WIDGET_TYPES, severityLabel, typeLabel } from './labels';

/* ============================================================
   全局反馈入口：顶栏右上角按钮 + 提交 Modal。
   支持图片粘贴 / 拖拽 / 选择，提交时自动带运行上下文（版本、路由等）。
   挂在顶栏（见 AppShell）。
   ============================================================ */

const MAX_IMAGES = 8;

interface LocalImage {
  key: string;
  file: File;
  url: string;
}

function newKey(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function FeedbackWidget() {
  const location = useLocation();
  const queryClient = useQueryClient();
  const { currentProject } = useProject();

  const [open, setOpen] = useState(false);
  const [type, setType] = useState<FeedbackType>('bug');
  const [severity, setSeverity] = useState<FeedbackSeverity>('normal');
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [images, setImages] = useState<LocalImage[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 卸载时释放所有预览 objectURL（用 ref 拿到最新列表）
  const imagesRef = useRef<LocalImage[]>([]);
  imagesRef.current = images;
  useEffect(() => () => imagesRef.current.forEach((i) => URL.revokeObjectURL(i.url)), []);

  const addFiles = (files: File[]) => {
    const imgs = files.filter((f) => f.type.startsWith('image/'));
    if (imgs.length === 0) return;
    setImages((prev) => {
      const room = MAX_IMAGES - prev.length;
      if (room <= 0) {
        toast(tr(`最多 ${MAX_IMAGES} 张图片`, `Up to ${MAX_IMAGES} images`), 'info');
        return prev;
      }
      if (imgs.length > room) {
        toast(tr(`最多 ${MAX_IMAGES} 张图片`, `Up to ${MAX_IMAGES} images`), 'info');
      }
      const added = imgs.slice(0, room).map((f) => ({ key: newKey(), file: f, url: URL.createObjectURL(f) }));
      return [...prev, ...added];
    });
  };

  const removeImage = (key: string) =>
    setImages((prev) => {
      const hit = prev.find((i) => i.key === key);
      if (hit) URL.revokeObjectURL(hit.url);
      return prev.filter((i) => i.key !== key);
    });

  const onPaste = (e: ClipboardEvent) => {
    const files: File[] = [];
    for (const item of Array.from(e.clipboardData.items)) {
      if (item.type.startsWith('image/')) {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) addFiles(files);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    addFiles(Array.from(e.dataTransfer.files));
  };

  const reset = () => {
    imagesRef.current.forEach((i) => URL.revokeObjectURL(i.url));
    setImages([]);
    setType('bug');
    setSeverity('normal');
    setTitle('');
    setBody('');
  };

  const close = () => {
    setOpen(false);
    reset();
  };

  const submit = useMutation({
    mutationFn: async () => {
      let version = '';
      try {
        version = (await api.health()).version;
      } catch {
        /* health 不可用不阻塞提交 */
      }
      const context: Record<string, unknown> = {
        version,
        env: import.meta.env.MODE,
        viewport: `${window.innerWidth}x${window.innerHeight}`,
        ua: navigator.userAgent,
        lang: getLang(),
        ...(currentProject?.name ? { project: currentProject.name } : {}),
      };
      const fb = await api.submitFeedback({
        type,
        severity,
        title: title.trim(),
        body: body.trim(),
        route: location.pathname,
        context,
      });
      for (const img of images) {
        try {
          await api.uploadFeedbackImage(fb.id, img.file);
        } catch {
          /* 单张图片上传失败不阻塞其余 */
        }
      }
      return fb;
    },
    onSuccess: () => {
      toast(tr('已提交，谢谢反馈', 'Thanks for the feedback'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-feedback'] });
      void queryClient.invalidateQueries({ queryKey: ['my-feedback'] });
      close();
    },
    onError: (e) => toast(`${tr('提交失败', 'Submit failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const canSubmit = title.trim() !== '' && !submit.isPending;

  return (
    <>
      {/* 顶栏入口 */}
      <button
        className="icon-btn"
        onClick={() => setOpen(true)}
        title={tr('反馈', 'Feedback')}
        aria-label={tr('反馈', 'Feedback')}
      >
        <Icon name="chat" size={16} />
      </button>

      <Modal
        open={open}
        onClose={close}
        width={560}
        title={
          <>
            <Icon name="chat" size={17} style={{ color: 'var(--accent)' }} />
            {tr('提交反馈', 'Send feedback')}
          </>
        }
        sub={tr('缺陷、功能建议、界面体验都欢迎；截图可直接粘贴', 'Bugs, feature ideas, UI issues welcome; paste screenshots directly')}
        footer={
          <>
            <button className="btn btn-ghost" onClick={close}>{tr('取消', 'Cancel')}</button>
            <button className="btn btn-primary" disabled={!canSubmit} onClick={() => submit.mutate()}>
              {submit.isPending ? tr('提交中…', 'Submitting…') : tr('提交', 'Submit')}
            </button>
          </>
        }
      >
        <div onPaste={onPaste} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={() => setDragOver(false)} onDrop={onDrop}>
          <div className="row gap12" style={{ alignItems: 'flex-start' }}>
            <FormField label={tr('类型', 'Type')} style={{ flex: 1 }}>
              <SelectMenu
                value={type}
                options={WIDGET_TYPES.map((t) => ({ value: t, label: typeLabel(t) }))}
                onChange={(v) => setType(v as FeedbackType)}
              />
            </FormField>
            <FormField label={tr('严重度', 'Severity')} style={{ flex: 1 }}>
              <SelectMenu
                value={severity}
                options={SEVERITIES.map((s) => ({ value: s, label: severityLabel(s) }))}
                onChange={(v) => setSeverity(v as FeedbackSeverity)}
              />
            </FormField>
          </div>
          <FormField label={tr('标题', 'Title')}>
            <input
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={tr('一句话概括问题或建议', 'One line summary')}
              maxLength={200}
            />
          </FormField>
          <FormField label={tr('描述', 'Description')} hint={tr('复现步骤、期望结果等；可直接粘贴 / 拖入截图', 'Repro steps, expected result…; paste or drop screenshots')}>
            <textarea
              className="textarea"
              style={{ minHeight: 120, resize: 'vertical' }}
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder={tr('详细描述…', 'Describe in detail…')}
            />
          </FormField>

          {/* 截图区 */}
          <div
            style={{
              border: `1px dashed ${dragOver ? 'var(--accent)' : 'var(--border-2)'}`,
              background: dragOver ? 'var(--accent-soft)' : 'var(--surface-2)',
              borderRadius: 10,
              padding: 12,
              transition: 'border-color .12s, background .12s',
            }}
          >
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: images.length > 0 ? 10 : 0 }}>
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                {tr('截图', 'Screenshots')} · {images.length}/{MAX_IMAGES}
              </span>
              <button
                className="btn btn-soft sm"
                type="button"
                disabled={images.length >= MAX_IMAGES}
                onClick={() => fileInputRef.current?.click()}
              >
                <Icon name="plus" size={12} />
                {tr('选择图片', 'Choose images')}
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                style={{ display: 'none' }}
                onChange={(e) => {
                  addFiles(Array.from(e.target.files ?? []));
                  e.target.value = '';
                }}
              />
            </div>
            {images.length > 0 && (
              <div className="row gap8" style={{ flexWrap: 'wrap' }}>
                {images.map((img) => (
                  <div key={img.key} style={{ position: 'relative', width: 76, height: 76 }}>
                    <img
                      src={img.url}
                      alt=""
                      style={{ width: 76, height: 76, objectFit: 'cover', borderRadius: 8, border: '0.5px solid var(--border)' }}
                    />
                    <button
                      type="button"
                      className="icon-btn"
                      title={tr('移除', 'Remove')}
                      onClick={() => removeImage(img.key)}
                      style={{
                        position: 'absolute',
                        top: -7,
                        right: -7,
                        width: 20,
                        height: 20,
                        borderRadius: '50%',
                        background: 'var(--surface)',
                        border: '0.5px solid var(--border)',
                        boxShadow: 'var(--shadow-card)',
                      }}
                    >
                      <Icon name="x" size={11} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </Modal>
    </>
  );
}
