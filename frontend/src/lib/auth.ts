import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

export const ACCESS_COOKIE = "uptora_access";
export const REFRESH_COOKIE = "uptora_refresh";
const backend = () => {
  const configured = process.env.DJANGO_API_URL;
  if (process.env.NODE_ENV === "production" && !configured)
    throw new Error("DJANGO_API_URL is required in production.");
  return configured ?? "http://localhost:8000";
};
const cookieBase = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};
export function validMutationOrigin(request: NextRequest) {
  const origin = request.headers.get("origin");
  const configured = process.env.NEXT_PUBLIC_APP_URL;
  if (process.env.NODE_ENV === "production" && !configured) return false;
  const expected = configured ?? request.nextUrl.origin;
  return origin === expected;
}
export async function setTokens(tokens: { access: string; refresh?: string }) {
  const jar = await cookies();
  jar.set(ACCESS_COOKIE, tokens.access, { ...cookieBase, maxAge: 15 * 60 });
  if (tokens.refresh)
    jar.set(REFRESH_COOKIE, tokens.refresh, {
      ...cookieBase,
      maxAge: 7 * 24 * 60 * 60,
    });
}
export async function clearTokens() {
  const jar = await cookies();
  jar.delete(ACCESS_COOKIE);
  jar.delete(REFRESH_COOKIE);
}
export async function django(path: string, init: RequestInit = {}) {
  return fetch(`${backend()}${path}`, {
    ...init,
    cache: "no-store",
    headers: { Accept: "application/json", ...init.headers },
  });
}
async function refresh() {
  const jar = await cookies(),
    token = jar.get(REFRESH_COOKIE)?.value;
  if (!token) return false;
  const response = await django("/api/auth/token/refresh/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh: token }),
  });
  if (!response.ok) {
    await clearTokens();
    return false;
  }
  const data = (await response.json()) as { access: string; refresh?: string };
  await setTokens(data);
  return true;
}
export async function authenticatedDjango(
  path: string,
  init: RequestInit = {},
) {
  const jar = await cookies();
  let access = jar.get(ACCESS_COOKIE)?.value;
  const response = await django(path, {
    ...init,
    headers: {
      ...init.headers,
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
    },
  });
  if (response.status !== 401) return response;
  if (!(await refresh())) return response;
  access = (await cookies()).get(ACCESS_COOKIE)?.value;
  return django(path, {
    ...init,
    headers: {
      ...init.headers,
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
    },
  });
}
export function csrfRejected() {
  return NextResponse.json(
    { detail: "This request did not originate from Uptora." },
    { status: 403 },
  );
}
