import { NextResponse } from "next/server";

const TOKEN = /^[A-Za-z0-9_-]{43,64}$/;

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ token: string }> },
) {
  const { token } = await params;
  if (!TOKEN.test(token))
    return NextResponse.json(
      { detail: "Report unavailable." },
      { status: 404 },
    );
  const base = process.env.DJANGO_API_URL ?? "http://127.0.0.1:8000";
  const response = await fetch(
    `${base}/api/public/incident-shares/${encodeURIComponent(token)}/`,
    { cache: "no-store" },
  );
  if (!response.ok)
    return NextResponse.json(
      { detail: "Report unavailable." },
      { status: 404 },
    );
  return NextResponse.json(await response.json(), {
    headers: {
      "Cache-Control": "private, no-store",
      "Referrer-Policy": "no-referrer",
      "X-Robots-Tag": "noindex, nofollow",
    },
  });
}
