import { useEffect, useState, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api';

/* 反馈截图：带鉴权（Bearer）的 blob 加载 —— 仿 components/ui/Avatar 的做法，
   useQuery 拿 blob → URL.createObjectURL，卸载时 revoke。 */
export function AuthImage({
  feedbackId,
  seq,
  alt,
  style,
  onClick,
}: {
  feedbackId: string;
  seq: number;
  alt?: string;
  style?: CSSProperties;
  onClick?: () => void;
}) {
  const { data: blob, isLoading } = useQuery({
    queryKey: ['feedback-image', feedbackId, seq],
    queryFn: () => api.feedbackImageBlob(feedbackId, seq),
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

  if (!url) {
    return (
      <div
        style={{
          background: 'var(--surface-3)',
          border: '0.5px solid var(--border)',
          borderRadius: 6,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 10,
          color: 'var(--text-4)',
          ...style,
        }}
      >
        {isLoading ? '…' : '×'}
      </div>
    );
  }
  return (
    <img
      src={url}
      alt={alt ?? ''}
      onClick={onClick}
      style={{ objectFit: 'cover', borderRadius: 6, border: '0.5px solid var(--border)', ...style }}
    />
  );
}
