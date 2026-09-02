import type { Metadata } from "next";
import { PublicIncidentReport } from "@/components/incidents/public-incident-report";

export const metadata: Metadata = {
  title: "Incident proof",
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

export default async function SharedIncidentPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  return <PublicIncidentReport token={token} />;
}
