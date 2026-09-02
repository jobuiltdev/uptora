import { NextRequest, NextResponse } from "next/server";
import { django, setTokens, validMutationOrigin } from "@/lib/auth";

export async function POST(request: NextRequest) {
  if (!validMutationOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin." },
      { status: 403 },
    );
  const credentials = await request.json();
  const registration = await django("/api/auth/register/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  });
  const registered = await registration.json();
  if (!registration.ok)
    return NextResponse.json(registered, { status: registration.status });
  const login = await django("/api/auth/login/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  });
  const tokens = await login.json();
  if (!login.ok)
    return NextResponse.json(
      { detail: "Account created. Please sign in." },
      { status: 409 },
    );
  await setTokens(tokens);
  return NextResponse.json(registered, { status: 201 });
}
