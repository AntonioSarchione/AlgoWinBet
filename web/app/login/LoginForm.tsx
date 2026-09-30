"use client";

import { useActionState, useState } from "react";
import { Eye, EyeOff, Loader2, LockKeyhole } from "lucide-react";
import { login, type LoginState } from "./actions";

export function LoginForm({ next }: { next: string }) {
  const [state, action, pending] = useActionState<LoginState, FormData>(login, {});
  const [show, setShow] = useState(false);

  return (
    <form action={action} className="login-form" noValidate>
      <input type="hidden" name="next" value={next} />
      <div className="field">
        <label htmlFor="password">Password</label>
        <div className="control" data-invalid={state.error ? "true" : undefined}>
          <LockKeyhole size={17} aria-hidden="true" />
          <input
            id="password"
            name="password"
            type={show ? "text" : "password"}
            autoComplete="current-password"
            autoFocus
            required
            aria-invalid={state.error ? true : undefined}
            aria-describedby={state.error ? "password-error" : undefined}
          />
          <button type="button" className="icon-btn" onClick={() => setShow((s) => !s)} aria-label={show ? "Nascondi password" : "Mostra password"}>
            {show ? <EyeOff size={17} aria-hidden="true" /> : <Eye size={17} aria-hidden="true" />}
          </button>
        </div>
        {state.error && (
          <p id="password-error" className="field-error" role="alert">{state.error}</p>
        )}
      </div>
      <button type="submit" className="btn btn-primary" disabled={pending} style={{ width: "100%" }}>
        {pending ? <Loader2 size={17} className="spin" aria-hidden="true" /> : null}
        {pending ? "Accesso in corso…" : "Entra"}
      </button>
    </form>
  );
}
