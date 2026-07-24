import { Avatar } from './Avatar';

/* ============================================================
   头像堆（facepile）：负 margin 叠放的小头像 + 超出部分 +N 圆片。
   点赞区、成员列表等通用；数据由外部传入，本组件纯展示。
   ============================================================ */

export interface FacepileUser {
  id: string;
  display_name: string;
  has_avatar: boolean;
}

const MAX_FACES = 4;

export function Facepile({
  users,
  total,
  size = 20,
  accentFirst = false,
}: {
  users: FacepileUser[];
  /** 总人数（含未在 users 预览里的），超出部分渲染 +N 圆片 */
  total: number;
  size?: number;
  /** 第一个头像描边用 accent 高亮（如「我赞过」时自己排第一） */
  accentFirst?: boolean;
}) {
  const shown = users.slice(0, MAX_FACES);
  const rest = total - shown.length;
  if (shown.length === 0) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', paddingLeft: 6 }}>
      {shown.map((u, i) => (
        <span
          key={u.id}
          title={u.display_name}
          style={{
            marginLeft: -6,
            zIndex: shown.length - i + 1,
            borderRadius: Math.max(3, Math.round(size * 0.14)) + 2,
            // 外圈描边分层：第一个（自己赞过时）用 accent 高亮
            boxShadow: `0 0 0 2px ${i === 0 && accentFirst ? 'var(--accent)' : 'var(--surface)'}`,
            display: 'flex',
            flexShrink: 0,
          }}
        >
          <Avatar userId={u.id} hasAvatar={u.has_avatar} name={u.display_name} size={size} />
        </span>
      ))}
      {rest > 0 && (
        <span
          className="mono"
          style={{
            marginLeft: -6,
            zIndex: 1,
            width: size,
            height: size,
            borderRadius: Math.max(3, Math.round(size * 0.14)),
            background: 'var(--surface-3)',
            color: 'var(--text-3)',
            fontSize: Math.max(8, Math.round(size * 0.42)),
            fontWeight: 650,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            boxShadow: '0 0 0 2px var(--surface)',
            flexShrink: 0,
          }}
        >
          +{rest}
        </span>
      )}
    </div>
  );
}
