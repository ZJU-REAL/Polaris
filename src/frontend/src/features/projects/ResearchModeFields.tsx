import { FormField } from '../../components/ui/FormField';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { tr } from '../../lib/i18n';
import type { ResearchMode } from './interdisciplinaryWorkflow';
import './interdisciplinary.css';

export function ResearchModeFields({
  mode,
  onModeChange,
}: {
  mode: ResearchMode;
  onModeChange: (mode: ResearchMode) => void;
}) {
  return (
    // 不自带卡片：放在新建课题表单那张卡里，跟名称、简介一组（卡片套卡片会多一层框）
    <div className="research-mode-fields">
      <FormField label={tr('课题类型', 'Topic type')}>
        <Segmented
          options={[
            { v: 'conventional' as const, label: tr('常规', 'Standard') },
            { v: 'interdisciplinary' as const, label: tr('跨学科', 'Interdisciplinary') },
          ]}
          value={mode}
          onChange={onModeChange}
        />
      </FormField>
      {mode === 'interdisciplinary' && (
        <div className="research-mode-note">
          <Icon name="layers" size={14} />
          <span>
            {tr(
              '先生成研究范围草案，你确认后才会保存并建立专属文献库。',
              'A scope draft is generated first. It’s saved, with its own library, only after you confirm.',
            )}
          </span>
        </div>
      )}
    </div>
  );
}
