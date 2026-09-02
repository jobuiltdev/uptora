import { NextRequest, NextResponse } from "next/server";
import { django, setTokens, validMutationOrigin } from "@/lib/auth";

export async function POST(request: NextRequest) {
  if (!validMutationOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin." },
      { status: 403 },
    );
  const credentials = await request.json();
  const login = await django("/api/auth/login/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  });
  const data = await login.json();
  if (!login.ok) return NextResponse.json(data, { status: login.status });
  await setTokens(data);
  const me = await django("/api/auth/me/", {
    headers: { Authorization: `Bearer ${data.access}` },
  });
  return NextResponse.json(await me.json(), { status: me.status });
}
