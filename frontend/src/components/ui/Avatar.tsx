import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api';

/* 用户头像：有上传头像时经带鉴权的 blob 加载；否则用 GitHub 式 identicon（据 seed 确定生成）。 */

/** FNV-1a 32 位哈希，稳定生成 identicon 图案与颜色。 */
function hashSeed(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** 头像统一为 GitHub 式圆角矩形（非圆形）。 */
function avatarRadius(size: number): number {
  return Math.max(4, Math.round(size * 0.25));
}

/** GitHub 式 identicon：5×5 左右对称像素块，浅底 + 单色。 */
function Identicon({ seed, size }: { seed: string; size: number }) {
  const { color, cell, out } = useMemo(() => {
    const h = hashSeed(seed);
    const hue = Math.floor((((h >>> 24) & 0xff) / 255) * 360); // 高位定色，与图案去相关
    const c = `hsl(${hue} 55% 52%)`;
    const cw = size / 5;
    const blocks: { x: number; y: number; key: string }[] = [];
    let bit = 0;
    for (let r = 0; r < 5; r++) {
      for (let col = 0; col < 3; col++) {
        const on = (h >> bit) & 1;
        bit++;
        if (!on) continue;
        const cols = col === 2 ? [2] : [col, 4 - col];
        for (const cc of cols) blocks.push({ x: cc * cw, y: r * cw, key: `${r}-${cc}` });
      }
    }
    return { color: c, cell: cw, out: blocks };
  }, [seed, size]);

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ borderRadius: avatarRadius(size), flexShrink: 0, display: 'block', background: '#edeef1' }}
      aria-hidden
    >
      {out.map((c) => (
        <rect key={c.key} x={c.x} y={c.y} width={cell + 0.5} height={cell + 0.5} fill={color} />
      ))}
    </svg>
  );
}

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

  if (url) {
    return (
      <img
        src={url}
        alt={name}
        style={{ width: size, height: size, borderRadius: avatarRadius(size), objectFit: 'cover', flexShrink: 0, display: 'block' }}
      />
    );
  }
  return <Identicon seed={userId || name || '?'} size={size} />;
}
