"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Activity, BookOpenCheck, CalendarDays, Gauge, LayoutDashboard, LogOut, Moon, Palette, Sparkles, Sun, Target, Wallet } from "lucide-react";
import PALETTES from "@/lib/palettes.json";
import { LogoMark, Wordmark } from "./Logo";
import { logout } from "@/app/login/actions";

const NAV = [
  { href: "/", label: "Home", icon: LayoutDashboard },
  { href: "/opportunita", label: "Opportunità", icon: Target },
  { href: "/palinsesto", label: "Palinsesto", icon: CalendarDays },
  { href: "/registro", label: "Registro", icon: BookOpenCheck },
  { href: "/bankroll", label: "Bankroll", icon: Wallet },
  { href: "/qualita", label: "Qualità del modello", icon: Gauge },
  { href: "/sistema", label: "Stato del sistema", icon: Activity },
];

export function Sidebar() {
  const path = usePathname();
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [palette, setPalette] = useState(""); // "" = AlgoWinBet's own colours

  useEffect(() => {
    setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
    setPalette(document.documentElement.dataset.palette ?? "");
  }, []);

  function pick(id: string) {
    setPalette(id);
    if (id) document.documentElement.dataset.palette = id;
    else delete document.documentElement.dataset.palette;
    try {
      if (id) localStorage.setItem("awb-palette", id);
      else localStorage.removeItem("awb-palette");
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }
  const current = PALETTES.find((x) => x.id === palette);

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
        <LogoMark size={38} id="side" />
        <span>
          <Wordmark />
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
        <label className="theme-btn palette-pick" title="Colori dell'app">
          <Palette size={17} aria-hidden="true" />
          <span>Colori: {current?.name ?? "AlgoWinBet"}</span>
          <span className="swatches" aria-hidden="true">
            {(current?.colors ?? ["#00e27a", "#00f2fe", "#4facfe"]).map((c) => <i key={c} style={{ background: c }} />)}
          </span>
          <select value={palette} onChange={(e) => pick(e.target.value)} aria-label="Colori dell'app">
            <option value="">AlgoWinBet (predefiniti)</option>
            {PALETTES.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </label>
        <button type="button" className="theme-btn" onClick={toggle} aria-label={theme === "dark" ? "Passa al tema chiaro" : "Passa al tema scuro"}>
          {theme === "dark" ? <Sun size={17} aria-hidden="true" /> : <Moon size={17} aria-hidden="true" />}
          <span>{theme === "dark" ? "Tema chiaro" : "Tema scuro"}</span>
        </button>
        <form action={logout}>
          <button type="submit" className="theme-btn" style={{ width: "100%" }} aria-label="Esci">
            <LogOut size={17} aria-hidden="true" />
            <span>Esci</span>
          </button>
        </form>
      </div>
    </aside>
  );
}
