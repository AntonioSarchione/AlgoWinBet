import { NextResponse, type NextRequest } from "next/server";

// Private dashboard: HTTP Basic auth against DASHBOARD_PASSWORD (Vercel project env var, set by the owner).
// Without the variable the site stays closed rather than open.
export function proxy(request: NextRequest) {
  const expected = process.env.DASHBOARD_PASSWORD;
  if (!expected) {
    return new NextResponse("Dashboard chiusa: imposta DASHBOARD_PASSWORD nelle variabili del progetto Vercel.", { status: 503 });
  }
  const header = request.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    const decoded = atob(header.slice(6));
    const password = decoded.slice(decoded.indexOf(":") + 1);
    if (password.length === expected.length && timingSafeEqual(password, expected)) {
      return NextResponse.next();
    }
  }
  return new NextResponse("Accesso riservato", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="AlgoWinBet", charset="UTF-8"' },
  });
}

function timingSafeEqual(a: string, b: string): boolean {
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
