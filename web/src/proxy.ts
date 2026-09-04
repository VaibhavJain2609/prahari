import { NextRequest, NextResponse } from "next/server";

// Coarse gate only: presence of the session cookie, not its validity — the
// BFF is the one thing that actually knows if a session is live, and every
// page here calls it (api.me()) on load and bounces to /login itself on a
// 401. This just stops an unauthenticated browser from ever rendering the
// board shell in the first place.
const SESSION_COOKIE_NAME = "prahari_session";

export function proxy(request: NextRequest) {
  const hasSession = request.cookies.has(SESSION_COOKIE_NAME);
  if (!hasSession) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", request.nextUrl.pathname);
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!login|api|_next/static|_next/image|favicon.ico).*)"],
};
