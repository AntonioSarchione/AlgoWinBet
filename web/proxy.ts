import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, verifySessionToken } from "./lib/session";

// Turso blocked the account on 2026-10-10 (free plan over its monthly quota) until the reset on 2026-11-01: every data page
// would fail. Online (production only) every page is served as /pausa, a notice that reads nothing; locally the dashboard
// still reads its database. Set to false together with DB_OFFLINE in app/api/tick/route.ts at the switch-over
// (docs/SITE_DB.md).
const DB_PAUSED = true;

// Private dashboard: every page needs a valid session cookie, obtained on /login with DASHBOARD_PASSWORD
// (Vercel project env var, set by the owner). Without the variable nobody can log in: the site stays closed.
export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const secret = process.env.DASHBOARD_PASSWORD;
  const loggedIn = Boolean(secret) && (await verifySessionToken(request.cookies.get(SESSION_COOKIE)?.value, secret!));

  if (pathname === "/login") {
    return loggedIn ? NextResponse.redirect(new URL("/", request.url)) : NextResponse.next();
  }
  if (loggedIn) {
    const paused = DB_PAUSED && process.env.VERCEL_ENV === "production" && pathname !== "/pausa";
    return paused ? NextResponse.rewrite(new URL("/pausa", request.url)) : NextResponse.next();
  }

  const url = new URL("/login", request.url);
  if (pathname !== "/") url.searchParams.set("next", pathname + search);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg|logos/|api/tick).*)"], // /api/tick checks its own secret
};
