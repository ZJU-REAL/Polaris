/* ============================================================
   把论文拖给 PolarisBuddy。

   自定义 MIME `application/x-polaris-paper` 而不是 text/plain：悬浮球只接平台自己
   拖出来的论文，从别处拖进来的文字/文件不会误触发解读。同时也放一份 text/plain，
   拖到外部编辑器里落下的是标题，不是一串 uuid。
   ============================================================ */

export const PAPER_DND_MIME = 'application/x-polaris-paper';

/** 展开到论文行/卡片上：`<div {...paperDragProps(p.id, p.title)}>`。 */
export function paperDragProps(paperId: string, title?: string) {
  return {
    draggable: true,
    onDragStart: (e: React.DragEvent) => {
      e.dataTransfer.setData(PAPER_DND_MIME, paperId);
      if (title) e.dataTransfer.setData('text/plain', title);
      e.dataTransfer.effectAllowed = 'copy';
    },
  };
}
