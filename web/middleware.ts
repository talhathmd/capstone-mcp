import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, verifySessionCookie } from "@/lib/auth";

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (
    pathname.startsWith("/login") ||
    pathname.startsWith("/api/auth/") ||
    pathname.startsWith("/_next") ||
    pathname === "/favicon.ico"
  ) {
    return NextResponse.next();
  }

  const authSecret = process.env.AUTH_SECRET;
  const password = process.env.COMPARE_PASSWORD;

  if (!authSecret || !password) {
    return new NextResponse(
      "Server misconfigured: set AUTH_SECRET and COMPARE_PASSWORD for the web app.",
      { status: 500, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!(await verifySessionCookie(token, authSecret, password))) {
    const login = new URL("/login", request.url);
    login.searchParams.set("from", pathname);
    return NextResponse.redirect(login);
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
