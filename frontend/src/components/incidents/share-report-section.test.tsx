import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as apiModule from "@/lib/api";
import type { IncidentShare } from "@/lib/types";
import { ShareReportSection } from "./share-report-section";

const share: IncidentShare = {
  created_at: "2026-09-02T10:00:00Z",
  expires_at: null,
  include_evidence: true,
  share_url: "https://uptora.test/share/incidents/opaque-token",
};

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ShareReportSection incidentId={7} />
    </QueryClientProvider>,
  );
}

describe("ShareReportSection", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("creates a share with the selected evidence setting", async () => {
    const api = vi
      .spyOn(apiModule, "api")
      .mockRejectedValueOnce(
        new apiModule.ApiError(404, { detail: "Not found." }),
      )
      .mockResolvedValueOnce(share);
    renderSection();
    const checkbox = await screen.findByRole("checkbox", {
      name: /include screenshot evidence/i,
    });
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "Create share link" }));
    expect(
      await screen.findByDisplayValue(share.share_url),
    ).toBeInTheDocument();
    expect(api).toHaveBeenLastCalledWith(
      "incidents/7/share",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"include_evidence":false'),
      }),
    );
  });

  it("requires explicit confirmation before revoking", async () => {
    const api = vi
      .spyOn(apiModule, "api")
      .mockResolvedValueOnce(share)
      .mockResolvedValueOnce(null)
      .mockRejectedValueOnce(
        new apiModule.ApiError(404, { detail: "Not found." }),
      );
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Revoke link" }));
    expect(api).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("incidents/7/share", {
        method: "DELETE",
      }),
    );
    expect(
      await screen.findByRole("button", { name: "Create share link" }),
    ).toBeVisible();
  });
});
