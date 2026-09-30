import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AlgoWinBet",
  description: "Analisi probabilistica personale: schedine o NO BET",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body>{children}</body>
    </html>
  );
}
