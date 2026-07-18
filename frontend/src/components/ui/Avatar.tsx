import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api';

/* 用户头像：有上传头像时经带鉴权的 blob 加载；否则显示名字首字母。 */

export function Avatar({
  userId,
  hasAvatar,
  name,
  size = 26,
  version = 0,
}: {
  userId: string | null | undefined;
  hasAvatar: boolean;
  name: string;
  size?: number;
  /** 上传新头像后自增以强制刷新 */
  version?: number;
}) {
  const { data: blob } = useQuery({
    queryKey: ['avatar', userId, version],
    queryFn: () => api.avatarBlob(userId!),
    enabled: !!userId && hasAvatar,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!blob) {
      setUrl(null);
      return;
    }
    const u = URL.createObjectURL(blob);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [blob]);

  const initial = (name || '?').trim().charAt(0).toUpperCase();
  if (url) {
    return (
      <img
        src={url}
        alt={name}
        style={{ width: size, height: size, borderRadius: '50%', objectFit: 'cover', flexShrink: 0, display: 'block' }}
      />
    );
  }
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: '50%',
        background: 'var(--grad-avatar)',
        color: 'var(--on-accent)',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: Math.max(10, size * 0.42),
        fontWeight: 600,
        flexShrink: 0,
      }}
    >
      {initial}
    </span>
  );
}
