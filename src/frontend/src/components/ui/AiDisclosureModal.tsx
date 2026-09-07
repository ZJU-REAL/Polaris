/* AI 使用披露声明弹窗（#691，设计报告 §18 信任设计③）。

   期刊要求作者披露 AI 使用且禁止 AI 署名——把这份合规负担做成一键生成：
   声明由后端从既有留痕确定性聚合渲染（零 LLM），前端只做风格/语言切换、
   复制与附录展开。稿件详情与 discovery 任务详情共用本组件（subject 二选一）。 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type AiDisclosureStyle } from '../../lib/api';
import { getLang, tr, type Lang } from '../../lib/i18n';
import { copyText } from '../../lib/clipboard';
import { Markdown } from '../../lib/markdown';
import { toast } from './Toast';
import { Modal } from './Modal';
import { Segmented } from './Segmented';

export interface AiDisclosureModalProps {
  open: boolean;
  onClose: () => void;
  /** 披露对象：稿件或任务（二选一，路由到对应端点）。 */
  subject: { kind: 'manuscript'; id: string } | { kind: 'voyage'; id: string };
}

export function AiDisclosureModal({ open, onClose, subject }: AiDisclosureModalProps) {
  const [style, setStyle] = useState<AiDisclosureStyle>('generic');
  // 声明语言独立于界面语言（界面中文也可能要投英文期刊），初始跟随界面
  const [lang, setLang] = useState<Lang>(getLang());
  const [showAppendix, setShowAppendix] = useState(false);

  const query = useQuery({
    queryKey: ['ai-disclosure', subject.kind, subject.id, style, lang],
    queryFn: () =>
      subject.kind === 'manuscript'
        ? api.getManuscriptAiDisclosure(subject.id, style, lang)
        : api.getVoyageAiDisclosure(subject.id, style, lang),
    enabled: open,
    staleTime: 60_000,
  });

  const data = query.data;

  // 顶层常量禁止 tr（语言切换不会重估），风格选项在渲染时构造
  const styleOptions = [
    { v: 'icmje' as AiDisclosureStyle, label: 'ICMJE' },
    { v: 'elsevier' as AiDisclosureStyle, label: 'Elsevier' },
    { v: 'generic' as AiDisclosureStyle, label: tr('通用', 'Generic') },
  ];

  const copy = async (text: string) => {
    if (await copyText(text)) toast(tr('已复制', 'Copied'));
    else toast(tr('复制失败', 'Copy failed'), 'error');
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={640}
      title={tr('AI 披露声明', 'AI use disclosure')}
      sub={tr(
        '按平台记录如实生成，可直接粘贴到投稿声明中',
        'Generated verbatim from platform records; paste into your submission statement',
      )}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            {tr('关闭', 'Close')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={!data}
            onClick={() => data && copy(data.statement)}
          >
            {tr('复制声明', 'Copy statement')}
          </button>
        </>
      }
    >
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <Segmented options={styleOptions} value={style} onChange={setStyle} />
        <Segmented
          options={[
            { v: 'zh' as Lang, label: '中文' },
            { v: 'en' as Lang, label: 'English' },
          ]}
          value={lang}
          onChange={setLang}
        />
      </div>

      {query.isLoading && (
        <div style={{ color: 'var(--text-3)', padding: '16px 0' }}>
          {tr('生成中…', 'Generating…')}
        </div>
      )}
      {query.isError && (
        <div style={{ color: 'var(--danger, #c00)', padding: '16px 0' }}>
          {tr('披露声明生成失败', 'Failed to generate the disclosure')}
        </div>
      )}
      {data && (
        <>
          <div
            style={{
              whiteSpace: 'pre-wrap',
              background: 'var(--surface-2)',
              border: '0.5px solid var(--border-2)',
              borderRadius: 8,
              padding: 12,
              fontSize: 13,
              lineHeight: 1.7,
              userSelect: 'text',
            }}
          >
            {data.statement}
          </div>

          {!data.facts.ai_used && (
            <div style={{ color: 'var(--text-3)', fontSize: 12, marginTop: 8 }}>
              {tr(
                '平台未记录到 AI 参与痕迹，声明如实说明未使用。',
                'No AI participation was recorded; the statement honestly reports none was used.',
              )}
            </div>
          )}

          <div style={{ marginTop: 12 }}>
            <button
              className="btn btn-ghost sm"
              onClick={() => setShowAppendix((v) => !v)}
            >
              {showAppendix
                ? tr('收起明细附录', 'Hide appendix')
                : tr('展开明细附录', 'Show appendix')}
            </button>
            {showAppendix && (
              <div
                style={{
                  marginTop: 8,
                  overflowX: 'auto',
                  border: '0.5px solid var(--border-2)',
                  borderRadius: 8,
                  padding: 12,
                  fontSize: 12,
                }}
              >
                <Markdown source={data.appendix} />
                <div style={{ marginTop: 8 }}>
                  <button className="btn btn-ghost sm" onClick={() => copy(data.appendix)}>
                    {tr('复制附录 Markdown', 'Copy appendix Markdown')}
                  </button>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
