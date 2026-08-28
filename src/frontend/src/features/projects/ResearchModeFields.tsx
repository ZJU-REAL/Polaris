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
    <div className="card card-pad research-mode-card">
      <FormField label={tr('研究方式', 'Research mode')}>
        <Segmented
          options={[
            { v: 'conventional' as const, label: tr('常规研究', 'Conventional') },
            { v: 'interdisciplinary' as const, label: tr('跨学科研究', 'Interdisciplinary') },
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
              'AI 先生成可编辑草案；只有在你确认后，范围版本和专属交叉文献库才会持久化。',
              'AI first proposes an editable draft. The versioned scope and dedicated evidence library are persisted only after confirmation.',
            )}
          </span>
        </div>
      )}
    </div>
  );
}
