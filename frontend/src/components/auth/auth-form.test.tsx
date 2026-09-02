import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthForm } from "./auth-form";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, refresh: vi.fn() }),
}));

describe("AuthForm", () => {
  beforeEach(() => {
    replace.mockReset();
    vi.restoreAllMocks();
  });

  it("submits registration through the same-origin auth handler", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(Response.json({ enabled: true }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 1, email: "owner@example.com" }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      );
    render(<AuthForm mode="register" />);
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "owner@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "long-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/register",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("shows a clean private-alpha state when registration is disabled", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(Response.json({ enabled: false }));
    render(<AuthForm mode="register" />);
    expect(
      await screen.findByRole("heading", { name: "Private alpha" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create account" }),
    ).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/registration-status", {
      cache: "no-store",
    });
  });

  it("shows a backend login failure without navigating", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "No active account found." }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<AuthForm mode="login" />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "owner@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No active account found.",
    );
    expect(replace).not.toHaveBeenCalled();
  });
});
