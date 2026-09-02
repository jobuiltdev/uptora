import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MonitorForm } from "./monitor-form";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

describe("MonitorForm", () => {
  it("reveals exact browser fields and the contact-form side-effect warning", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MonitorForm websiteId={1} />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText("Monitor type"), {
      target: { value: "BROWSER" },
    });
    expect(
      screen.getByRole("textbox", { name: /Expected text/ }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Monitor type"), {
      target: { value: "FLOW" },
    });
    expect(
      screen.getByText("This check submits a real form."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Test-safe value/)).toHaveAttribute(
      "type",
      "password",
    );
  });
});
