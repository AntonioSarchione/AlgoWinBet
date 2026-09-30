import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import { pct } from "./format";

// Club monogram: a stable hue from the name (identity decoration only, the name is always printed next to it).
export function TeamBadge({ name, size = "sm" }: { name: string; size?: "sm" | "lg" }) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const words = name.replace(/[^\p{L}\p{N} ]/gu, "").split(" ").filter((w) => w.length > 1 && !/^(fc|ac|as|ss|sc|cf|afc|us|vfb|vfl|rc|ud|cd|sl)$/i.test(w));
  const initials = ((words[0]?.[0] ?? name[0]) + (words[1]?.[0] ?? words[0]?.[1] ?? "")).toUpperCase();
  return (
    <span className={`badge${size === "lg" ? " badge-lg" : ""}`} style={{ background: `hsl(${h % 360} 45% 34%)` }} aria-hidden="true">
      {initials}
    </span>
  );
}

export function MatchCell({ home, away, sub, href }: { home: string; away: string; sub?: React.ReactNode; href?: string }) {
  const body = (
    <span className="match-cell">
      <span className="badges">
        <TeamBadge name={home} />
        <TeamBadge name={away} />
      </span>
      <span style={{ minWidth: 0 }}>
        <b>
          {home} <span className="muted">vs</span> {away}
        </b>
        {sub && <small>{sub}</small>}
      </span>
    </span>
  );
  return href ? (
    <Link href={href} className="row-link">
      {body}
    </Link>
  ) : (
    body
  );
}

export function Empty({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children?: React.ReactNode }) {
  return (
    <div className="empty">
      <Icon size={32} strokeWidth={1.5} aria-hidden="true" />
      <b>{title}</b>
      {children && <p>{children}</p>}
    </div>
  );
}

// 1X2 split as one stacked bar (2px gaps). Percentages are always printed next to it: colour is never the only cue.
export function Split1X2({ h, d, a }: { h: number | null; d: number | null; a: number | null }) {
  if (h == null || d == null || a == null) return <span className="muted">–</span>;
  return (
    <span className="stack" role="img" aria-label={`1 ${pct(h)}, X ${pct(d)}, 2 ${pct(a)}`} title={`1 ${pct(h)} · X ${pct(d)} · 2 ${pct(a)}`}>
      <span style={{ width: `${h * 100}%`, background: "var(--s1)" }} />
      <span style={{ width: `${d * 100}%`, background: "var(--s2)" }} />
      <span style={{ width: `${a * 100}%`, background: "var(--s3)" }} />
    </span>
  );
}

export function Ring({ value, label, sub, color }: { value: number | null; label: string; sub?: string; color: string }) {
  const r = 42;
  const c = 2 * Math.PI * r;
  const v = value ?? 0;
  return (
    <div className="ring">
      <svg viewBox="0 0 100 100" role="img" aria-label={`${label}: ${pct(value, 1)}`}>
        <circle cx="50" cy="50" r={r} fill="none" stroke="var(--track)" strokeWidth="9" />
        <circle
          cx="50" cy="50" r={r} fill="none" stroke={color} strokeWidth="9" strokeLinecap="round"
          strokeDasharray={`${Math.max(v * c - 2, 0)} ${c}`} transform="rotate(-90 50 50)"
        />
        <text x="50" y="55" textAnchor="middle" fontSize="17" fontWeight="700" fill="var(--text)" style={{ fontFamily: "var(--font-mono)" }}>
          {pct(value, 1)}
        </text>
      </svg>
      <b>{label}</b>
      {sub && <small>{sub}</small>}
    </div>
  );
}

export function Meter({ used, limit, label, hint }: { used: number; limit: number; label: string; hint?: string }) {
  const share = Math.min(used / limit, 1);
  const tone = share >= 0.9 ? "bad" : share >= 0.7 ? "warn" : "";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div className="kv" style={{ padding: 0 }}>
        <span>{label}</span>
        <span className="num">
          {used} / {limit}
        </span>
      </div>
      <div className={`meter ${tone}`} role="meter" aria-valuenow={used} aria-valuemin={0} aria-valuemax={limit} aria-label={label}>
        <span style={{ width: `${share * 100}%` }} />
      </div>
      {hint && <span className="note">{hint}</span>}
    </div>
  );
}

export function HBar({ label, p, max = 1 }: { label: string; p: number; max?: number }) {
  return (
    <div className="hbar">
      <span className="text-2">{label}</span>
      <span className="bar">
        <span style={{ width: `${Math.min(p / max, 1) * 100}%` }} />
      </span>
      <span className="num">{pct(p, 1)}</span>
    </div>
  );
}
