import { safeNext } from "@/lib/session";
import { LoginForm } from "./LoginForm";
import { LogoMark, Wordmark } from "@/app/_components/Logo";

export const metadata = { title: "Accesso" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const next = safeNext((await searchParams).next);
  return (
    <main className="login">
      <section className="card login-card" aria-labelledby="login-title">
        <LogoMark size={72} id="login" />
        <h1 id="login-title"><Wordmark /></h1>
        <p className="muted">Dati · Analisi · Opportunità</p>
        <LoginForm next={next} />
      </section>
    </main>
  );
}
