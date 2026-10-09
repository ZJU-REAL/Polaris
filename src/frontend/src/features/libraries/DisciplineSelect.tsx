/* ============================================================
   学科口径选择框（建库弹窗与库设置卡共用）。

   抽出来共用不是为了少写几行：三条提示——包已卸载 / 包里没有 schema /
   一个包都没装——每一条都是在说破一个「选了但不会生效」的状态。复制一份，
   下次改的时候必然只改一处，另一处继续对用户说一件没发生的事。
   ============================================================ */
import { useQuery } from '@tanstack/react-query';
import { api, type DisciplinePackSummary } from '../../lib/api';
import { tr } from '../../lib/i18n';

/**
 * 选择框的值 → 发给后端的 discipline。
 *
 * 空串代表「通用（不限学科）」，必须发 **null** 而不是 ""：后端拿 "" 去比对已装的
 * 包名，匹配不到就按未知学科 400 掉。界面上的表现是「想清空学科，保存却失败」，
 * 而错误信息说的是一个用户从没输入过的值。
 */
export function disciplineFieldValue(selected: string): string | null {
  return selected || null;
}

export interface DisciplineSelectProps {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  /** 建库弹窗里横向空间有限，不必撑到 360。 */
  maxWidth?: number;
  /** 紧凑：只留选择框（与「已移除」警告），说明交给所在行。设置页的行内用。 */
  compact?: boolean;
}

export function DisciplineSelect({ value, onChange, disabled, maxWidth = 360, compact }: DisciplineSelectProps) {
  const packs = useQuery({
    queryKey: ['disciplines'],
    queryFn: () => api.listDisciplines(),
    retry: false,
  });

  const options: DisciplinePackSummary[] = packs.data ?? [];
  const current = options.find((p) => p.name === value);
  // 存着一个已经被卸载的包名：不补这一项的话，下拉框找不到匹配值会显示成空白，
  // 看起来像「通用」，而库里其实还存着那个名字、抽取又确实不按它走。得说破。
  const missing = !!value && !packs.isLoading && !current;

  if (packs.isError) {
    return (
      <p className="muted" style={{ fontSize: 12 }}>
        {tr('无法加载学科列表。', 'Couldn’t load disciplines.')}
      </p>
    );
  }

  return (
    <div className="col gap8" style={{ width: maxWidth, maxWidth: '100%' }}>
      <select
        className="input"
        value={value}
        disabled={disabled || packs.isLoading}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: '100%' }}
      >
        <option value="">{tr('通用', 'General')}</option>
        {missing && (
          <option value={value}>
            {value}
            {tr('（已移除）', ' (removed)')}
          </option>
        )}
        {options.map((p) => (
          <option key={p.name} value={p.name}>
            {tr(p.title, p.title_en || p.title)}
          </option>
        ))}
      </select>
      {!compact && (current?.description || current?.description_en) && current && (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {tr(current.description, current.description_en || current.description)}
        </p>
      )}
      {missing && (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {tr(
            '这个学科包已被移除，暂按通用字段提取。请重新安装或改选其他学科。',
            'This discipline pack was removed, so general fields are used. Reinstall it or choose another.',
          )}
        </p>
      )}
      {/* 没有自己的抽取 schema 的包（如计算机科学）只换文献口径，方法卡沿用内置字段。
          说清楚，免得人以为选了它方法卡会变 */}
      {!compact && current && current.schema_count === 0 && !(current.description || current.description_en) && (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {tr(
            '方法卡使用通用字段，学科只影响默认来源和 arXiv 分类。',
            'Method cards use the general fields. The discipline sets default sources and arXiv categories.',
          )}
        </p>
      )}
      {!packs.isLoading && options.length === 0 && (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {tr(
            '还没有安装学科包。',
            'No discipline packs installed.',
          )}
        </p>
      )}
    </div>
  );
}
