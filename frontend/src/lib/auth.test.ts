import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieState = vi.hoisted(() => {
  const values = new Map<string, string>();
  const jar = {
    get: vi.fn((name: string) => {
      const value = values.get(name);
      return value ? { name, value } : undefined;
    }),
    set: vi.fn((name: string, value: string) => values.set(name, value)),
    delete: vi.fn((name: string) => values.delete(name)),
  };
  return { values, jar, cookies: vi.fn(async () => jar) };
});

vi.mock("next/headers", () => ({ cookies: cookieState.cookies }));

import { POST as login } from "@/app/api/auth/login/route";
import {
  ACCESS_COOKIE,
  authenticatedDjango,
  REFRESH_COOKIE,
  validMutationOrigin,
} from "./auth";

describe("BFF authentication security", () => {
  beforeEach(() => {
    cookieState.values.clear();
    cookieState.jar.get.mockClear();
    cookieState.jar.set.mockClear();
    cookieState.jar.delete.mockClear();
    process.env.DJANGO_API_URL = "http://django.test";
    process.env.NEXT_PUBLIC_APP_URL = "https://app.uptora.test";
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.DJANGO_API_URL;
    delete process.env.NEXT_PUBLIC_APP_URL;
  });

  it("rejects an invalid Origin in the actual mutating login route", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const request = new NextRequest("https://app.uptora.test/api/auth/login", {
      method: "POST",
      headers: {
        origin: "https://attacker.test",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        email: "owner@example.com",
        password: "not-a-real-secret",
      }),
    });
    const response = await login(request);
    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("persists both access and rotated refresh tokens before replaying a 401 once", async () => {
    cookieState.values.set(ACCESS_COOKIE, "expired-access");
    cookieState.values.set(REFRESH_COOKIE, "current-refresh");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(
        Response.json({ access: "new-access", refresh: "rotated-refresh" }),
      )
      .mockResolvedValueOnce(
        Response.json({ id: 7, email: "owner@example.com" }),
      );

    const response = await authenticatedDjango("/api/auth/me/");

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(cookieState.values.get(ACCESS_COOKIE)).toBe("new-access");
    expect(cookieState.values.get(REFRESH_COOKIE)).toBe("rotated-refresh");
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toMatchObject({
      Authorization: "Bearer new-access",
    });
  });

  it("keeps the pure Origin comparison aligned with the configured frontend", () => {
    const request = (origin: string) =>
      ({
        headers: new Headers({ origin }),
        nextUrl: new URL("https://app.uptora.test/api/bff/websites"),
      }) as unknown as NextRequest;
    expect(validMutationOrigin(request("https://attacker.test"))).toBe(false);
    expect(validMutationOrigin(request("https://app.uptora.test"))).toBe(true);
  });
});
