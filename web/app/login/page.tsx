import { TrendingUp } from "lucide-react";
import { safeNext } from "@/lib/session";
import { LoginForm } from "./LoginForm";

export const metadata = { title: "Accesso" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const next = safeNext((await searchParams).next);
  return (
    <main className="login">
      <section className="card login-card" aria-labelledby="login-title">
        <span className="brand-mark login-mark">
          <TrendingUp size={24} strokeWidth={2.4} aria-hidden="true" />
        </span>
        <h1 id="login-title">AlgoWinBet</h1>
        <p className="muted">Dati · Analisi · Opportunità</p>
        <LoginForm next={next} />
      </section>
    </main>
  );
}
