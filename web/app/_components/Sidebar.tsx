"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Activity, CalendarDays, LayoutDashboard, Moon, Sparkles, Sun, Target, TrendingUp } from "lucide-react";

const NAV = [
  { href: "/", label: "Home", icon: LayoutDashboard },
  { href: "/opportunita", label: "Opportunità", icon: Target },
  { href: "/palinsesto", label: "Palinsesto", icon: CalendarDays },
  { href: "/sistema", label: "Stato del sistema", icon: Activity },
];

export function Sidebar() {
  const path = usePathname();
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
  }, []);

  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("awb-theme", next);
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }

  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href) || (href === "/palinsesto" && path.startsWith("/partita")));

  return (
    <aside className="sidebar">
      <Link href="/" className="brand" aria-label="AlgoWinBet, home">
        <span className="brand-mark">
          <TrendingUp size={19} strokeWidth={2.4} aria-hidden="true" />
        </span>
        <span>
          <b>AlgoWinBet</b>
          <small>Dati · Analisi · Opportunità</small>
        </span>
      </Link>
      <nav className="nav" aria-label="Sezioni">
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link key={href} href={href} aria-current={active(href) ? "page" : undefined} title={label}>
            <Icon size={18} aria-hidden="true" />
            <span>{label}</span>
          </Link>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className="promo">
          <Sparkles size={18} color="var(--accent)" aria-hidden="true" />
          <b>Probabilità, non certezze</b>
          Modello statistico + prezzi di mercato. Solo paper trading: nessuna scommessa viene piazzata.
        </div>
        <button type="button" className="theme-btn" onClick={toggle} aria-label={theme === "dark" ? "Passa al tema chiaro" : "Passa al tema scuro"}>
          {theme === "dark" ? <Sun size={17} aria-hidden="true" /> : <Moon size={17} aria-hidden="true" />}
          <span>{theme === "dark" ? "Tema chiaro" : "Tema scuro"}</span>
        </button>
      </div>
    </aside>
  );
}
