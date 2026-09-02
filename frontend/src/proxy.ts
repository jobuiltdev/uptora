import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, REFRESH_COOKIE } from "@/lib/auth";

export function proxy(request: NextRequest) {
  const signedIn =
    request.cookies.has(ACCESS_COOKIE) || request.cookies.has(REFRESH_COOKIE);
  const auth =
    request.nextUrl.pathname === "/login" ||
    request.nextUrl.pathname === "/register";
  if (!signedIn && !auth) {
    const url = new URL("/login", request.url);
    url.searchParams.set("next", request.nextUrl.pathname);
    return NextResponse.redirect(url);
  }
  if (signedIn && auth)
    return NextResponse.redirect(new URL("/dashboard", request.url));
  return NextResponse.next();
}
export const config = {
  matcher: [
    "/dashboard/:path*",
    "/websites/:path*",
    "/incidents/:path*",
    "/settings/:path*",
    "/login",
    "/register",
  ],
};
