import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PublicIncidentReport as PublicIncidentReportData } from "@/lib/types";
import {
  IncidentReportView,
  PublicIncidentReport,
} from "./public-incident-report";

const base: PublicIncidentReportData = {
  website_name: "Client site",
  website_url: "https://client.example",
  monitor_type: "BROWSER",
  status: "OPEN",
  started_at: "2026-09-02T10:00:00Z",
  resolved_at: null,
  duration_seconds: 600,
  failure_type: "EXPECTED_TEXT_MISSING",
  failure_summary: "Expected page content was not found.",
  latest_failure_summary: "Expected page content was not found.",
  status_code: 200,
  failure_count: 2,
  recovery_count: 0,
  timeline: [
    {
      occurred_at: "2026-09-02T10:00:00Z",
      outcome: "FAILURE",
      label: "Initial failure",
      summary: "Expected page content was not found.",
      status_code: 200,
      evidence_available: false,
    },
  ],
  timeline_total_count: 1,
  evidence_available: false,
  captured_at: "2026-09-02T10:01:00Z",
};

function renderPublic(token = "invalid-token") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PublicIncidentReport token={token} />
    </QueryClientProvider>,
  );
}

describe("public incident report", () => {
  afterEach(() => vi.restoreAllMocks());

  it("uses one generic unavailable state for invalid or expired links", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 404 }),
    );
    renderPublic();
    expect(
      await screen.findByRole("heading", { name: "Report unavailable" }),
    ).toBeVisible();
    expect(screen.queryByText(/expired|revoked/i)).not.toBeInTheDocument();
  });

  it("clearly renders an ongoing incident", () => {
    render(<IncidentReportView report={base} token="opaque-token" />);
    expect(screen.getByText("Ongoing incident")).toBeVisible();
    expect(screen.queryByText("Resolved incident")).not.toBeInTheDocument();
  });

  it("clearly renders a resolved incident", () => {
    render(
      <IncidentReportView
        report={{
          ...base,
          status: "RESOLVED",
          resolved_at: "2026-09-02T10:10:00Z",
        }}
        token="opaque-token"
      />,
    );
    expect(screen.getByText("Resolved incident")).toBeVisible();
  });

  it("does not render evidence when the share excludes it", () => {
    render(<IncidentReportView report={base} token="opaque-token" />);
    expect(
      screen.queryByRole("img", { name: /screenshot captured/i }),
    ).not.toBeInTheDocument();
  });
});
