import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Modal } from '../../components/ui/Modal';
import { Icon } from '../../components/ui/Icon';
import type { DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { AddToLibraryModal } from '../library/AddToLibraryModal';
import { AddPapersModal } from '../wiki/AddPapersModal';
import { libraryPath } from './hooks';

/* ============================================================
   「添加论文」的唯一入口（论文首页）。先选加到哪里，再打开那里原有的添加方式：

     我的文献库   一篇一篇：arXiv / DOI / 链接 / 标题（AddToLibraryModal）
     某个文献库   一次最多 50 篇：arXiv / DOI / PMID / BibTeX / Zotero（AddPapersModal）

   两套添加的内部不动，只把入口合成一个。
   ============================================================ */

type Target = { kind: 'mine' } | { kind: 'library'; id: string };

export function AddPapersEntry({
  open,
  onClose,
  libraries,
}: {
  open: boolean;
  onClose: () => void;
  libraries: DirectionLibrarySummary[];
}) {
  const navigate = useNavigate();
  const [target, setTarget] = useState<Target>({ kind: 'mine' });
  const [step, setStep] = useState<'choose' | 'mine' | 'library'>('choose');

  useEffect(() => {
    if (open) {
      setStep('choose');
      setTarget({ kind: 'mine' });
    }
  }, [open]);

  const options: { key: string; target: Target; title: string; hint: string }[] = [
    {
      key: 'mine',
      target: { kind: 'mine' },
      title: tr('我的文献库', 'My library'),
      hint: tr('自己收藏的论文，按 arXiv 编号、DOI、链接或标题加', 'Your own saved papers; add by arXiv ID, DOI, link or title'),
    },
    ...libraries.map((l) => ({
      key: l.id,
      target: { kind: 'library', id: l.id } as Target,
      title: l.name,
      hint: tr(
        `${l.paper_count} 篇论文；可一次粘贴多篇编号或 BibTeX，或从 Zotero 导入`,
        `${l.paper_count} papers; paste several IDs or BibTeX, or import from Zotero`,
      ),
    })),
  ];
  const isOn = (t: Target) => (t.kind === 'mine' ? target.kind === 'mine' : target.kind === 'library' && target.id === t.id);

  return (
    <>
      <Modal
        open={open && step === 'choose'}
        onClose={onClose}
        title={tr('添加论文', 'Add papers')}
        sub={tr('加到哪里', 'Where to add them')}
        width={520}
        footer={
          <>
            <button className="btn btn-ghost sm" onClick={onClose}>
              {tr('取消', 'Cancel')}
            </button>
            <button
              className="btn btn-primary sm"
              data-testid="add-entry-next"
              onClick={() => setStep(target.kind === 'mine' ? 'mine' : 'library')}
            >
              {tr('下一步', 'Next')}
            </button>
          </>
        }
      >
        <div className="ae-list" role="radiogroup" aria-label={tr('加到哪里', 'Where to add them')}>
          {options.map((o) => (
            <label key={o.key} className={'ae-opt' + (isOn(o.target) ? ' is-on' : '')} data-testid={`add-entry-${o.key}`}>
              <input type="radio" name="add-target" checked={isOn(o.target)} onChange={() => setTarget(o.target)} />
              <span className="ae-text">
                <b title={o.title}>{o.title}</b>
                <small>{o.hint}</small>
              </span>
            </label>
          ))}
        </div>
        <p className="ae-note">
          <Icon name="file" size={12} />
          {tr('手上只有 PDF？先按标题或编号加进来，再在论文详情里上传 PDF。', 'Only have the PDF? Add the paper by title or ID first, then upload the PDF in its details.')}
        </p>
      </Modal>
      <AddToLibraryModal
        open={open && step === 'mine'}
        onClose={onClose}
        onAdded={() => {
          onClose();
          navigate('/library');
        }}
      />
      {target.kind === 'library' && (
        <AddPapersModal
          libraryId={target.id}
          open={open && step === 'library'}
          onClose={onClose}
          onImported={(paperId) => navigate(libraryPath(target.id, `?paper=${paperId}`))}
        />
      )}
    </>
  );
}
