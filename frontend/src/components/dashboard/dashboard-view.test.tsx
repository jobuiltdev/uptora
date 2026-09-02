import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Dashboard } from "@/lib/types";
import { OnboardingChecklist } from "./dashboard-view";

const emptyDashboard: Dashboard = {
  summary: {
    websites: 0,
    enabled_monitors: 0,
    healthy_monitors: 0,
    active_incidents: 0,
    checks_24h: 0,
  },
  websites: [],
  active_incidents: [],
  recent_checks: [],
};

describe("OnboardingChecklist", () => {
  it("shows the actionable first-run sequence and explains incident/report concepts", () => {
    render(<OnboardingChecklist data={emptyDashboard} />);
    expect(
      screen.getByRole("heading", { name: "Getting started" }),
    ).toBeVisible();
    expect(screen.getByText(/Add your first website/)).toBeVisible();
    expect(screen.getByText(/Create a monitor/)).toBeVisible();
    expect(screen.getByText(/Run and understand a check/)).toBeVisible();
    expect(
      screen.getByText(/Two consecutive failures open an incident/),
    ).toBeVisible();
    expect(screen.getByText(/share a read-only client report/)).toBeVisible();
  });

  it("disappears after the core first-run steps are complete", () => {
    const complete = {
      ...emptyDashboard,
      summary: {
        ...emptyDashboard.summary,
        websites: 1,
        enabled_monitors: 1,
        checks_24h: 1,
      },
    };
    const { container } = render(<OnboardingChecklist data={complete} />);
    expect(container).toBeEmptyDOMElement();
  });
});
