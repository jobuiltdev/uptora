import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import {
  clearTokens,
  django,
  REFRESH_COOKIE,
  validMutationOrigin,
} from "@/lib/auth";

export async function POST(request: NextRequest) {
  if (!validMutationOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin." },
      { status: 403 },
    );
  const refresh = (await cookies()).get(REFRESH_COOKIE)?.value;
  if (refresh)
    await django("/api/auth/logout/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh }),
    }).catch(() => null);
  await clearTokens();
  return new NextResponse(null, { status: 204 });
}
