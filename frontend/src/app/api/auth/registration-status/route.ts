import { NextResponse } from "next/server";
import { django } from "@/lib/auth";

export async function GET() {
  try {
    const response = await django("/api/auth/registration-status/");
    if (!response.ok) throw new Error("registration status unavailable");
    return NextResponse.json(await response.json(), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ enabled: false }, { status: 503 });
  }
}
