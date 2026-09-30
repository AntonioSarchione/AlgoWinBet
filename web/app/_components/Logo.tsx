// Brand mark from images/algowinbet-logo.svg: the hexagon with the growth line, cropped to a square.
// The banner's background, tagline and "HIGH WIN-RATE" badge are left out: the mark sits on the app's own surfaces
// and the tool makes no win-rate claim. `id` keeps gradient/filter ids unique when the mark appears twice on a page.
export function LogoMark({ size = 36, id = "lm" }: { size?: number; id?: string }) {
  const g = (n: string) => `${id}-${n}`;
  return (
    <svg width={size} height={size} viewBox="28 38 184 184" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={g("algo")} x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor="#00F2FE" />
          <stop offset="100%" stopColor="#4FACFE" />
        </linearGradient>
        <linearGradient id={g("win")} x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#00FF87" />
          <stop offset="100%" stopColor="#60EFFF" />
        </linearGradient>
        <linearGradient id={g("border")} x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#00F2FE" stopOpacity="0.9" />
          <stop offset="50%" stopColor="#00FF87" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#3B82F6" stopOpacity="0.25" />
        </linearGradient>
        <linearGradient id={g("fill")} x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stopColor="#00FF87" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#00F2FE" stopOpacity="0" />
        </linearGradient>
        <filter id={g("glow")} x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <polygon points="120,45 190,85 190,175 120,215 50,175 50,85" fill="#0F172A" stroke={`url(#${g("border")})`} strokeWidth="5" />
      <g opacity="0.3" fill={`url(#${g("algo")})`}>
        <rect x="75" y="140" width="8" height="25" rx="2" />
        <rect x="90" y="125" width="8" height="40" rx="2" />
        <rect x="105" y="110" width="8" height="55" rx="2" />
        <rect x="120" y="130" width="8" height="35" rx="2" />
        <rect x="135" y="95" width="8" height="70" rx="2" />
        <rect x="150" y="80" width="8" height="85" rx="2" />
      </g>
      <polygon points="72,150 92,130 112,142 142,92 168,70 168,168 72,168" fill={`url(#${g("fill")})`} />
      <g fill="none" stroke={`url(#${g("win")})`} strokeWidth="7" strokeLinecap="round" strokeLinejoin="round" filter={`url(#${g("glow")})`}>
        <path d="M 72 150 L 92 130 L 112 142 L 142 92 L 168 70" />
        <path d="M 150 70 L 170 68 L 168 88" />
        <path d="M 98 180 L 112 192 L 142 162" strokeWidth="6" />
      </g>
      <circle cx="72" cy="150" r="5" fill="#00F2FE" />
      <circle cx="92" cy="130" r="5" fill="#00F2FE" />
      <circle cx="112" cy="142" r="5" fill="#00F2FE" />
      <circle cx="142" cy="92" r="5" fill="#00FF87" />
    </svg>
  );
}

// Wordmark: "Algo" cyan, "Win" green, "Bet" in the text colour (white on dark, ink on light), as in the banner.
export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={`wordmark ${className ?? ""}`}>
      <span className="wm-algo">Algo</span>
      <span className="wm-win">Win</span>
      <span className="wm-bet">Bet</span>
    </span>
  );
}
