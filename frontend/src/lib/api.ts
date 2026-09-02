export class ApiError extends Error {
  constructor(
    public status: number,
    public data: unknown,
  ) {
    super(messageFrom(data));
  }
}
function messageFrom(data: unknown): string {
  if (
    data &&
    typeof data === "object" &&
    "detail" in data &&
    typeof data.detail === "string"
  )
    return data.detail;
  if (data && typeof data === "object") {
    for (const value of Object.values(data)) {
      if (Array.isArray(value) && typeof value[0] === "string") return value[0];
      if (value && typeof value === "object") {
        const nested = messageFrom(value);
        if (nested !== "The request could not be completed.") return nested;
      }
    }
  }
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
