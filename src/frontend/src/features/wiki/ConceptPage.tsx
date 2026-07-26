import { useCallback } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { ConceptDetailPane } from './ConceptsTab';
import { conceptPath, pickConceptByName } from './shared';

/* ============================================================
   /concepts/:id — 概念页。概念是平台级实体（论文级上链，不属于任何课题），
   所以和 /papers/:id/read 一样放在课题作用域外。
   - `?library=<id>`：从某个库的上下文点进来，关联论文只列这个库里的；
   - 不带：每日推送 / 个人库 / 相关研究这类池级上下文，关联论文列全平台的。
   库内的概念库 tab 仍在库工作台里就地展示，两边共用 ConceptDetailPane。
   ============================================================ */

export function ConceptPage() {
  const { id = '' } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const libraryId = searchParams.get('library') ?? undefined;

  const openPaper = useCallback((paperId: string) => navigate(`/papers/${paperId}/read`), [navigate]);
  // 概念之间互跳沿用当前视角：从库里进来的继续看这个库，池级的继续看全平台
  const openConcept = useCallback(
    (conceptId: string) => navigate(conceptPath(conceptId, libraryId)),
    [navigate, libraryId],
  );
  // 正文里的 [[双链]] 只给名字：走平台级按名查（与其它池级入口同一套），没入库就提示
  const onWikiLink = useCallback(
    async (name: string) => {
      try {
        const hit = pickConceptByName(await api.lookupConcept(name), name);
        if (!hit) {
          toast(tr(`概念「${name}」还没入库`, `“${name}” is not in the knowledge base yet`), 'info');
          return;
        }
        navigate(conceptPath(hit.id, libraryId));
      } catch {
        toast(tr('概念解析失败（后端不可用）', 'Concept lookup failed (backend unavailable)'), 'error');
      }
    },
    [navigate, libraryId],
  );

  return (
    <div className="page fadeup" style={{ maxWidth: 900 }}>
      <button className="btn btn-soft sm" onClick={() => navigate(-1)} style={{ marginBottom: 16 }}>
        <Icon name="arrow" size={13} style={{ transform: 'rotate(180deg)' }} />
        {tr('返回', 'Back')}
      </button>
      <div className="card" style={{ display: 'flex', overflow: 'hidden' }}>
        {id ? (
          <ConceptDetailPane
            conceptId={id}
            libraryId={libraryId}
            onOpenPaper={openPaper}
            onOpenConcept={openConcept}
            onWikiLink={onWikiLink}
          />
        ) : (
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载概念详情', 'Failed to load concept')}
            desc={tr('链接里没有概念 id。', 'The link carries no concept id.')}
          />
        )}
      </div>
    </div>
  );
}
