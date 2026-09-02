import { NextRequest, NextResponse } from "next/server";
import { authenticatedDjango, validMutationOrigin } from "@/lib/auth";

const allowed = [
  /^dashboard\/?$/,
  /^websites(?:\/\d+)?\/?$/,
  /^monitors(?:\/\d+)?\/?$/,
  /^monitors\/\d+\/(?:run|results)\/?$/,
  /^incidents(?:\/\d+)?\/?$/,
  /^notifications\/preferences\/?$/,
  /^check-results\/\d+\/evidence\/?$/,
];
async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const path = (await params).path.join("/");
  if (!allowed.some((rule) => rule.test(path)))
    return NextResponse.json(
      { detail: "Unknown Uptora resource." },
      { status: 404 },
    );
  if (request.method !== "GET" && !validMutationOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin." },
      { status: 403 },
    );
  const query = request.nextUrl.search;
  const body =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();
  const response = await authenticatedDjango(`/api/${path}/${query}`, {
    method: request.method,
    body,
    headers: {
      ...(request.headers.get("content-type")
        ? { "Content-Type": request.headers.get("content-type")! }
        : {}),
    },
  });
  const headers = new Headers();
  const contentType = response.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  const disposition = response.headers.get("content-disposition");
  if (disposition) headers.set("Content-Disposition", disposition);
  return new NextResponse(
    response.status === 204 ? null : await response.arrayBuffer(),
    { status: response.status, headers },
  );
}
export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
