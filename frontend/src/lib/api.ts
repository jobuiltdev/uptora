export class ApiError extends Error {
  constructor(
    public status: number,
    public data: unknown,
  ) {
    super(messageFrom(data));
  }
}
function messageFrom(data: unknown) {
  if (
    data &&
    typeof data === "object" &&
    "detail" in data &&
    typeof data.detail === "string"
  )
    return data.detail;
  return "The request could not be completed.";
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/bff/${path.replace(/^\//, "")}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const data =
    response.status === 204
      ? null
      : contentType.includes("json")
        ? await response.json()
        : await response.text();
  if (!response.ok) throw new ApiError(response.status, data);
  return data as T;
}
