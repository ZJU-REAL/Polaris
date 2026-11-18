import { Segmented } from '../../components/ui/Segmented';
import { tr } from '../../lib/i18n';
import type { ResearchMode } from './interdisciplinaryWorkflow';
import './interdisciplinary.css';

/** 课题类型：常规 / 跨学科。放在课题设置的「课题类型」那张卡里（不自带卡片）。 */
export function ResearchModeFields({
  mode,
  onModeChange,
}: {
  mode: ResearchMode;
  onModeChange: (mode: ResearchMode) => void;
}) {
  return (
    <div className="research-mode-fields" data-research-mode={mode}>
      <div style={{ display: 'inline-flex' }}>
        <Segmented
          options={[
            { v: 'conventional' as const, label: tr('常规', 'Standard') },
            { v: 'interdisciplinary' as const, label: tr('跨学科', 'Interdisciplinary') },
          ]}
          value={mode}
          onChange={onModeChange}
        />
      </div>
      <div className="hint" style={{ marginTop: 8 }}>
        {mode === 'interdisciplinary'
          ? tr(
              '跨学科课题多一份研究范围（在这一页最上面）：先生成草案，你确认后才会保存并建立专属文献库。',
              'An interdisciplinary topic has a research scope (at the top of this page): a draft is generated first, and it is saved with its own library only after you confirm.',
            )
          : tr('研究横跨两个以上学科时选「跨学科」，会多一份研究范围和一个专属文献库。', 'Choose Interdisciplinary when the research spans two or more fields; it adds a research scope and a dedicated library.')}
      </div>
    </div>
  );
}
