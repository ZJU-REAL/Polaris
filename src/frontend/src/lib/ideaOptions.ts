import type { IdeaRead } from './api';
import { tr } from './i18n';

/* 新建实验 / 新建稿件里的「想法」下拉：列出所有没被淘汰的想法，已选中的排前面。
   以前只列 status=promoted，没选中过任何想法的课题在这两处是空的——
   而后端早就允许从任意想法开始（实验创建时会顺手把它选中）。 */
export function selectableIdeaOptions(
  ideas: Pick<IdeaRead, 'id' | 'title' | 'status'>[],
): { value: string; label: string }[] {
  const usable = ideas.filter((i) => i.status !== 'rejected');
  const picked = usable.filter((i) => i.status === 'promoted');
  const rest = usable.filter((i) => i.status !== 'promoted');
  return [
    // 标记放在前面：下拉框窄，放在标题后面会被省略号截掉
    ...picked.map((i) => ({ value: i.id, label: `${tr('已选中', 'Picked')} · ${i.title}` })),
    ...rest.map((i) => ({ value: i.id, label: i.title })),
  ];
}
