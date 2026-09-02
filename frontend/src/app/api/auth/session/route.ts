import { NextResponse } from "next/server";
import { authenticatedDjango, clearTokens } from "@/lib/auth";

export async function GET() {
  const response = await authenticatedDjango("/api/auth/me/");
  if (!response.ok) {
    await clearTokens();
    return NextResponse.json(
      { detail: "Authentication required." },
      { status: 401 },
    );
  }
  return NextResponse.json(await response.json());
}
