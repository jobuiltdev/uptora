import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Status } from "./status";

describe("Status", () => {
  it("communicates health with text as well as color", () => {
    render(<Status value="DEGRADED" />);
    expect(screen.getByText("Pending confirmation")).toBeInTheDocument();
  });
});
