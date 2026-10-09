import type { Metadata, Viewport } from "next";
import { Barlow, Barlow_Condensed, Barlow_Semi_Condensed } from "next/font/google";
import "./globals.css";
import "./palettes.css";
import PALETTES from "@/lib/palettes.json";

// UI UX Pro Max "Sports/Fitness" pairing: Barlow Condensed for headings, Barlow for text, Semi Condensed for figures.
const sans = Barlow({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-body", display: "swap" });
const display = Barlow_Condensed({ subsets: ["latin"], weight: ["600", "700", "800"], variable: "--font-display-face", display: "swap" });
const figures = Barlow_Semi_Condensed({ subsets: ["latin"], weight: ["500", "600", "700"], variable: "--font-figures", display: "swap" });

export const metadata: Metadata = {
  title: { default: "AlgoWinBet", template: "%s · AlgoWinBet" },
  description: "Analisi probabilistica personale: schedine o NO BET",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { themeColor: "#050a0d", colorScheme: "dark light" };

// Applies the saved theme (dark / light) and colours (a bookmaker palette, palettes.css) before first paint (no flash).
// Dark and the AlgoWinBet colours are the default.
const THEME_SCRIPT = `try{var d=document.documentElement,t=localStorage.getItem("awb-theme"),p=localStorage.getItem("awb-palette");`
  + `if(t==="light")d.dataset.theme="light";if(${JSON.stringify(PALETTES.map((x) => x.id))}.indexOf(p)>=0)d.dataset.palette=p}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it" className={`${sans.variable} ${display.variable} ${figures.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
