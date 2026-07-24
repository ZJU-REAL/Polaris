import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { PaperStatusPill } from '../../components/ui/StatusPill';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { EmptyState } from '../../components/ui/EmptyState';
import { FigureEmbed, FiguresSection, hasEmbeddedFigures, usePaperFigures } from '../../components/ui/FigureGallery';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { clickable } from '../../lib/a11y';
import { api, type PaperDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { topicPath } from '../../app/project';

/* ============================================================
   阅读工作台 · 论文信息面板（PaperDetailPane 的精简版）：
   元数据卡 + 摘要折叠 + 概念/标签 chips + wiki 正文 markdown。
   ============================================================ */

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 10, padding: '3px 0', alignItems: 'flex-start' }}>
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', width: 78, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: 'var(--text-2)', flex: 1, minWidth: 0, overflowWrap: 'break-word' }}>
        {children}
      </span>
    </div>
  );
}

export function InfoPanel({
  paper,
  onWikiLink,
}: {
  paper: PaperDetail;
  /** [[概念]] 双链与概念 chips 点击 → 按名称跳 wiki 概念库 */
  onWikiLink: WikiLinkHandler;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [abstractOpen, setAbstractOpen] = useState(false);
  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null;
  const extUrl = arxivUrl ?? paper.url;

  // —— 个人版 wiki（P5b）：没有库版解读时，读本人个人库条目里的个人编译版 ——
  const libStateQuery = useQuery({
    queryKey: ['library-state', paper.id],
    queryFn: () => api.getLibraryState(paper.id),
    enabled: !paper.wiki_content,
    retry: false,
  });
  const entryId = libStateQuery.data?.entry_id ?? null;
  const entryQuery = useQuery({
    queryKey: ['library-entry', entryId],
    queryFn: () => api.getLibraryEntry(entryId!),
    enabled: !paper.wiki_content && !!entryId,
    retry: false,
  });
  const personalWiki = paper.wiki_content ? null : (entryQuery.data?.wiki_content ?? null);
  const generateMutation = useMutation({
    mutationFn: () => api.compilePersonalWiki(paper.id, paper.project_id),
    onSuccess: () => {
      toast(tr('个人版解读已生成', 'Personal wiki generated'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library-state', paper.id] });
      void queryClient.invalidateQueries({ queryKey: ['library-entry'] });
      void queryClient.invalidateQueries({ queryKey: ['library'] });
    },
    onError: (e) =>
      toast(`${tr('生成失败：', 'Failed to generate: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 正文 ![[fig:N]] 嵌入图（docs/api-lit.md §6.6）
  const figures = usePaperFigures(paper);
  const paperId = paper.id;
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig ? <FigureEmbed paperId={paperId} fig={fig} /> : null;
    },
    [figures, paperId],
  );

  return (
    <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 16px 40px' }}>
      {/* —— 头部 —— */}
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        <PaperStatusPill status={paper.status} sm />
        {paper.venue && (
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {paper.venue}
          </span>
        )}
        {typeof paper.note_count === 'number' && paper.note_count > 0 && (
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            <Icon name="pen" size={10} />
            {paper.note_count} {tr('条笔记', 'notes')}
          </span>
        )}
      </div>
      <div style={{ fontSize: 14.5, fontWeight: 660, lineHeight: 1.4, marginBottom: 5 }}>{paper.title}</div>
      {paper.authors.length > 0 && (
        <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.6 }}>
          {paper.authors.map((a, i) => {
            const affil = a.affiliations?.filter(Boolean) ?? [];
            return (
              <span key={`${a.name}-${i}`}>
                {i > 0 && <span style={{ color: 'var(--text-4)' }}> · </span>}
                <span
                  className="author-link"
                  title={
                    affil.length > 0
                      ? `${a.name} — ${affil.join('; ')}`
                      : tr(`回文献库只看 ${a.name} 的论文`, `Back to the library, showing only ${a.name}'s papers`)
                  }
                  {...clickable(() => navigate(topicPath(paper.project_id, `wiki?author=${encodeURIComponent(a.name)}`)))}
                >
                  {a.name}
                </span>
                {affil.length > 0 && (
                  <span style={{ color: 'var(--text-4)', fontSize: 10.5 }}> ({affil[0]}{affil.length > 1 ? ` +${affil.length - 1}` : ''})</span>
                )}
              </span>
            );
          })}
        </div>
      )}
      {(paper.affiliations?.length ?? 0) > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 8 }}>
          <Icon name="pin" size={10} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
          {paper.affiliations!.map((name) => (
            <span
              key={name}
              className="chip"
              style={{ fontSize: 10.5, height: 20 }}
              title={tr(`回文献库只看 ${name} 的论文`, `Back to the library, showing only papers from ${name}`)}
              {...clickable(() => navigate(topicPath(paper.project_id, `wiki?affiliation=${encodeURIComponent(name)}`)))}
            >
              {name}
            </span>
          ))}
        </div>
      )}
      {extUrl && (
        <a
          className="btn btn-ghost sm"
          href={extUrl}
          target="_blank"
          rel="noreferrer noopener"
          style={{ textDecoration: 'none', marginTop: 10, display: 'inline-flex' }}
        >
          <Icon name="link" size={12} />
          {arxivUrl ? tr('arXiv 原文', 'View on arXiv') : tr('原文链接', 'Source link')}
        </a>
      )}

      {/* —— 元数据卡 —— */}
      <div className="card" style={{ margin: '14px 0 0', background: 'var(--surface-2)', padding: '10px 14px' }}>
        <MetaItem label="arxiv_id">
          {paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="doi">
          {paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="published">
          {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="relevance">
          {paper.relevance_score !== null ? (
            <RelevanceBar value={paper.relevance_score} width={120} />
          ) : (
            <span className="muted">{tr('未打分', 'Not scored')}</span>
          )}
        </MetaItem>
        <MetaItem label="ingested">
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
      </div>

      {/* —— 标签 —— */}
      {(paper.tags?.length ?? 0) > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 12 }}>
          {paper.tags!.map((t) => (
            <span key={t} className="tag">
              {t}
            </span>
          ))}
        </div>
      )}

      {/* —— 概念 chips —— */}
      {paper.concepts.length > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 12 }}>
          {paper.concepts.map((c) => (
            <span key={c.id} className="wikilink" style={{ height: 22 }} {...clickable(() => onWikiLink(c.name))}>
              {c.name}
            </span>
          ))}
        </div>
      )}

      {/* —— 摘要（折叠） —— */}
      {paper.abstract && (
        <div className="card" style={{ marginTop: 14, overflow: 'hidden' }}>
          <div
            className="row"
            onClick={() => setAbstractOpen((o) => !o)}
            style={{ padding: '9px 13px', cursor: 'pointer', justifyContent: 'space-between', userSelect: 'none' }}
          >
            <span style={{ fontSize: 12, fontWeight: 650 }}>
              {tr('摘要', 'Abstract')}
            </span>
            <Icon
              name="chevDown"
              size={13}
              style={{
                color: 'var(--text-3)',
                transform: abstractOpen ? 'rotate(180deg)' : 'none',
                transition: 'transform .15s',
              }}
            />
          </div>
          {abstractOpen && (
            <div style={{ padding: '0 13px 12px', fontSize: 12, lineHeight: 1.7, color: 'var(--text-2)' }}>
              {paper.abstract}
            </div>
          )}
        </div>
      )}

      {/* —— TL;DR —— */}
      {paper.tldr && (
        <div
          style={{
            marginTop: 14,
            padding: '10px 13px',
            borderRadius: 10,
            background: 'var(--accent-soft)',
            fontSize: 12.5,
            lineHeight: 1.65,
          }}
        >
          <span className="mono" style={{ fontSize: 10, color: 'var(--accent-text)', display: 'block', marginBottom: 3 }}>
            TL;DR
          </span>
          {paper.tldr}
        </div>
      )}

      {/* —— 重要图片画廊（正文已嵌图时默认折叠，避免重复视觉） —— */}
      <FiguresSection
        paper={paper}
        style={{ marginTop: 14 }}
        defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)}
      />

      {/* —— Wiki 正文（含 ![[fig:N]] 嵌入图）：库版 > 个人版 > 生成入口 —— */}
      <div style={{ marginTop: 18 }}>
        {paper.wiki_content ? (
          <>
            <div
              className="row gap8"
              style={{ paddingBottom: 8, marginBottom: 12, borderBottom: '0.5px solid var(--border)' }}
            >
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
                {tr('AI 图文介绍', 'AI intro')}
              </span>
              <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
            </div>
            <Markdown
              source={paper.wiki_content}
              onWikiLink={onWikiLink}
              renderFigure={renderFigure}
              style={{ fontSize: 12.5 }}
            />
          </>
        ) : personalWiki ? (
          <>
            <div
              className="row gap8"
              style={{ paddingBottom: 8, marginBottom: 12, borderBottom: '0.5px solid var(--border)' }}
            >
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
                {tr('AI 图文介绍', 'AI intro')}
              </span>
              <span
                className="mono"
                title={tr('公共库里没有这篇的解读，这是你自己生成的个人版。', 'No shared library wiki; this is the personal version you generated.')}
                style={{
                  fontSize: 10,
                  color: 'var(--accent-text)',
                  background: 'var(--accent-soft)',
                  padding: '1px 7px',
                  borderRadius: 999,
                }}
              >
                {tr('个人版', 'Personal')}
              </span>
            </div>
            <Markdown
              source={personalWiki}
              onWikiLink={onWikiLink}
              renderFigure={renderFigure}
              style={{ fontSize: 12.5 }}
            />
          </>
        ) : (
          <>
            <EmptyState
              compact
              icon="pen"
              title={tr('还没有 AI 精读页', 'No AI deep-read page yet')}
              desc={tr('这篇论文还没被 AI 精读整理过（相关度不足，或还没运行初始建库 / 增量同步）。', 'This paper has not been deep-read by AI yet (relevance too low, or initial library build / incremental sync has not run).')}
            />
            <div className="col" style={{ alignItems: 'center', gap: 6, marginTop: 10 }}>
              <button
                className="btn btn-soft sm"
                disabled={generateMutation.isPending || libStateQuery.isLoading || entryQuery.isLoading}
                onClick={() => generateMutation.mutate()}
              >
                <Icon name="sparkle" size={12} />
                {generateMutation.isPending ? tr('生成中…（约 1 分钟）', 'Generating… (~1 min)') : tr('生成 wiki', 'Generate wiki')}
              </button>
              <span style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
                {tr('将使用你的模型额度，生成个人版解读。', 'Uses your model quota to generate a personal wiki.')}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
