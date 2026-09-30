import type { Metadata, Viewport } from "next";
import { Fira_Code, Fira_Sans } from "next/font/google";
import "./globals.css";

const sans = Fira_Sans({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-fira-sans", display: "swap" });
const mono = Fira_Code({ subsets: ["latin"], weight: ["500", "600", "700"], variable: "--font-fira-code", display: "swap" });

export const metadata: Metadata = {
  title: { default: "AlgoWinBet", template: "%s · AlgoWinBet" },
  description: "Analisi probabilistica personale: schedine o NO BET",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { themeColor: "#050a0d", colorScheme: "dark light" };

// Applies the saved theme before first paint (no flash). Dark is the default.
const THEME_SCRIPT = `try{var t=localStorage.getItem("awb-theme");if(t==="light")document.documentElement.dataset.theme="light"}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it" className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
