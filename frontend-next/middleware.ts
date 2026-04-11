import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

const PUBLIC_PATHS = new Set(["/login"]);

export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  if (pathname.startsWith("/_next") || pathname.startsWith("/api") || pathname === "/favicon.ico") {
    return NextResponse.next();
  }

  const hasSessionCookie = Boolean(request.cookies.get("app_refresh_token")?.value || request.cookies.get("app_access_token")?.value);
  const isPublic = PUBLIC_PATHS.has(pathname);

  if (!hasSessionCookie && !isPublic) {
    const loginUrl = new URL("/login", request.url);
    const nextValue = `${pathname}${search}`;
    if (nextValue && nextValue !== "/") {
      loginUrl.searchParams.set("next", nextValue);
    }
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!.*\\..*).*)"],
};
