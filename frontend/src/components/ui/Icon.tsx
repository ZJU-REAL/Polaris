import type { CSSProperties, ReactElement } from 'react';

export type IconName =
  | 'dashboard' | 'book' | 'bulb' | 'scale' | 'flask' | 'pen' | 'shield' | 'gate'
  | 'bell' | 'search' | 'settings' | 'plus' | 'chevron' | 'chevDown' | 'arrow'
  | 'link' | 'check' | 'x' | 'play' | 'pause' | 'clock' | 'cpu' | 'server'
  | 'file' | 'git' | 'chart' | 'grid' | 'layers' | 'sparkle' | 'refresh'
  | 'logout' | 'dot' | 'compass' | 'users' | 'trash'
  | 'star' | 'starFill' | 'chat' | 'download' | 'sliders' | 'sidebar' | 'minus';

export interface IconProps {
  name: IconName;
  size?: number;
  sw?: number;
  style?: CSSProperties;
}

/** Minimal inline stroke icon set (currentColor). */
export function Icon({ name, size = 17, sw = 1.6, style }: IconProps) {
  const p = {
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: sw,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
  } as const;
  const paths: Record<IconName, ReactElement> = {
    dashboard: <><rect x="3" y="3" width="7" height="9" rx="1.5" {...p} /><rect x="14" y="3" width="7" height="5" rx="1.5" {...p} /><rect x="14" y="12" width="7" height="9" rx="1.5" {...p} /><rect x="3" y="16" width="7" height="5" rx="1.5" {...p} /></>,
    book: <><path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H19a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H5.5A1.5 1.5 0 0 0 4 21.5z" {...p} /><path d="M4 17.5A1.5 1.5 0 0 1 5.5 16H20" {...p} /></>,
    bulb: <><path d="M9 18h6M10 21h4" {...p} /><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.3 1 2.1h5c0-.8.4-1.6 1-2.1A6 6 0 0 0 12 3z" {...p} /></>,
    scale: <><path d="M12 3v18M7 21h10" {...p} /><path d="M5 7h14M5 7l-2.5 5a2.5 2.5 0 0 0 5 0zM19 7l-2.5 5a2.5 2.5 0 0 0 5 0z" {...p} /><path d="M5 7l7-2 7 2" {...p} /></>,
    flask: <><path d="M9 3h6M10 3v6l-4.5 8A2 2 0 0 0 7.3 20h9.4a2 2 0 0 0 1.8-3L14 9V3" {...p} /><path d="M7.5 15h9" {...p} /></>,
    pen: <><path d="M12 20h9" {...p} /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" {...p} /></>,
    shield: <><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z" {...p} /><path d="M9 12l2 2 4-4" {...p} /></>,
    gate: <><path d="M6 21V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v15" {...p} /><path d="M3 21h18M9 21V11h6v10M9 8h6" {...p} /></>,
    bell: <><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" {...p} /><path d="M13.7 21a2 2 0 0 1-3.4 0" {...p} /></>,
    search: <><circle cx="11" cy="11" r="7" {...p} /><path d="M21 21l-4-4" {...p} /></>,
    settings: <><path d="M21.88 10.44A10.0 10.0 0 0 1 21.88 13.56L18.79 14.08A7.1 7.1 0 0 1 18.27 15.33L20.09 17.88A10.0 10.0 0 0 1 17.88 20.09L15.33 18.27A7.1 7.1 0 0 1 14.08 18.79L13.56 21.88A10.0 10.0 0 0 1 10.44 21.88L9.92 18.79A7.1 7.1 0 0 1 8.67 18.27L6.12 20.09A10.0 10.0 0 0 1 3.91 17.88L5.73 15.33A7.1 7.1 0 0 1 5.21 14.08L2.12 13.56A10.0 10.0 0 0 1 2.12 10.44L5.21 9.92A7.1 7.1 0 0 1 5.73 8.67L3.91 6.12A10.0 10.0 0 0 1 6.12 3.91L8.67 5.73A7.1 7.1 0 0 1 9.92 5.21L10.44 2.12A10.0 10.0 0 0 1 13.56 2.12L14.08 5.21A7.1 7.1 0 0 1 15.33 5.73L17.88 3.91A10.0 10.0 0 0 1 20.09 6.12L18.27 8.67A7.1 7.1 0 0 1 18.79 9.92L21.88 10.44Z" {...p} /><circle cx="12" cy="12" r="3.4" {...p} /></>,
    plus: <><path d="M12 5v14M5 12h14" {...p} /></>,
    minus: <><path d="M5 12h14" {...p} /></>,
    sidebar: <><rect x="3" y="4" width="18" height="16" rx="2" {...p} /><path d="M9 4v16" {...p} /></>,
    chevron: <><path d="M9 6l6 6-6 6" {...p} /></>,
    chevDown: <><path d="M6 9l6 6 6-6" {...p} /></>,
    arrow: <><path d="M5 12h14M13 6l6 6-6 6" {...p} /></>,
    link: <><path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1" {...p} /><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1" {...p} /></>,
    check: <><path d="M4 12l5 5L20 6" {...p} /></>,
    x: <><path d="M6 6l12 12M18 6L6 18" {...p} /></>,
    play: <><path d="M7 5l11 7-11 7z" {...p} /></>,
    pause: <><path d="M8 5v14M16 5v14" {...p} /></>,
    clock: <><circle cx="12" cy="12" r="9" {...p} /><path d="M12 7v5l3 2" {...p} /></>,
    cpu: <><rect x="6" y="6" width="12" height="12" rx="2" {...p} /><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" {...p} /><rect x="10" y="10" width="4" height="4" rx="1" {...p} /></>,
    server: <><rect x="3" y="4" width="18" height="7" rx="2" {...p} /><rect x="3" y="13" width="18" height="7" rx="2" {...p} /><path d="M7 7.5h.01M7 16.5h.01" {...p} /></>,
    file: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" {...p} /><path d="M14 3v5h5" {...p} /></>,
    git: <><circle cx="6" cy="6" r="2.5" {...p} /><circle cx="6" cy="18" r="2.5" {...p} /><circle cx="18" cy="9" r="2.5" {...p} /><path d="M6 8.5v7M18 11.5c0 3-3 3.5-6 3.5" {...p} /></>,
    chart: <><path d="M4 20V4M4 20h16" {...p} /><path d="M7 16l4-5 3 3 4-7" {...p} /></>,
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" {...p} /><rect x="14" y="3" width="7" height="7" rx="1.5" {...p} /><rect x="3" y="14" width="7" height="7" rx="1.5" {...p} /><rect x="14" y="14" width="7" height="7" rx="1.5" {...p} /></>,
    layers: <><path d="M12 3l9 5-9 5-9-5z" {...p} /><path d="M3 13l9 5 9-5M3 17l9 5 9-5" {...p} opacity="0.5" /></>,
    sparkle: <><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" {...p} /></>,
    refresh: <><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5" {...p} /></>,
    logout: <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" {...p} /><path d="M16 17l5-5-5-5M21 12H9" {...p} /></>,
    dot: <circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" />,
    compass: <><circle cx="12" cy="12" r="9" {...p} /><path d="M15.5 8.5l-2.2 5-4.8 2 2.2-5z" {...p} /></>,
    users: <><circle cx="9" cy="8" r="3.5" {...p} /><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6" {...p} /><circle cx="17" cy="9" r="2.5" {...p} /><path d="M16.5 14.6c2.6.6 4.5 2.8 4.5 5.4" {...p} /></>,
    trash: <><path d="M4 7h16M10 7V4h4v3M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" {...p} /><path d="M10 11v7M14 11v7" {...p} /></>,
    star: <path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9-4.3-4.1 5.9-.8z" {...p} />,
    starFill: <path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9-4.3-4.1 5.9-.8z" fill="currentColor" stroke="currentColor" strokeWidth={sw} strokeLinejoin="round" />,
    chat: <><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5c-1.6 0-3-.4-4.3-1.1L3 20l1.1-5.2A8.5 8.5 0 1 1 21 11.5z" {...p} /><path d="M8 10.5h8M8 13.5h5" {...p} /></>,
    sliders: <><path d="M3 5.8h9.4 M17.8 5.8H21 M3 12h2.6 M10.6 12H21 M3 18.2h11.4 M19.8 18.2H21" {...p} /><circle cx="15.1" cy="5.8" r="2.1" {...p} /><circle cx="7.6" cy="12" r="2.1" {...p} /><circle cx="17.1" cy="18.2" r="2.1" {...p} /></>,
    download: <><path d="M12 3v11M7 10l5 5 5-5" {...p} /><path d="M4 20h16" {...p} /></>,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: 'block', ...style }}>
      {paths[name]}
    </svg>
  );
}
