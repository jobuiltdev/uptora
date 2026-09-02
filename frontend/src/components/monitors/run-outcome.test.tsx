import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CheckResult } from "@/lib/types";
import { RunOutcome } from "./run-outcome";

const failedResult: CheckResult = {
  id: 4,
  monitor: 2,
  checked_at: "2026-09-02T10:00:00Z",
  is_success: false,
  status_code: 503,
  response_time_ms: 41,
  error_type: "HTTP_ERROR",
  error_message: "HTTP 503",
  final_url: "https://example.test/",
  screenshot: null,
  ssl_expires_at: null,
  ssl_days_remaining: null,
};

describe("RunOutcome", () => {
  it("renders an observed target failure as a completed check, not an infrastructure error", () => {
    render(<RunOutcome result={failedResult} />);
    expect(screen.getByText("Target check failed")).toBeInTheDocument();
    expect(screen.getByText("HTTP 503")).toBeInTheDocument();
    expect(
      screen.queryByText(/could not reach Uptora/i),
    ).not.toBeInTheDocument();
  });
});
