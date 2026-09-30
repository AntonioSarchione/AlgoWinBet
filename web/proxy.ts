import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, verifySessionToken } from "./lib/session";

// Private dashboard: every page needs a valid session cookie, obtained on /login with DASHBOARD_PASSWORD
// (Vercel project env var, set by the owner). Without the variable nobody can log in: the site stays closed.
export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const secret = process.env.DASHBOARD_PASSWORD;
  const loggedIn = Boolean(secret) && (await verifySessionToken(request.cookies.get(SESSION_COOKIE)?.value, secret!));

  if (pathname === "/login") {
    return loggedIn ? NextResponse.redirect(new URL("/", request.url)) : NextResponse.next();
  }
  if (loggedIn) return NextResponse.next();

  const url = new URL("/login", request.url);
  if (pathname !== "/") url.searchParams.set("next", pathname + search);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"],
};
