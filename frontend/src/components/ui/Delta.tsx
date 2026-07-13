import type { ReactNode } from 'react';

export interface DeltaProps {
  children: ReactNode;
  down?: boolean;
}

/** 指标增减量标签。 */
export function Delta({ children, down }: DeltaProps) {
  return (
    <span className={down ? 'delta-down' : 'delta-up'}>
      {down ? '▼ ' : '▲ '}
      {children}
    </span>
  );
}
