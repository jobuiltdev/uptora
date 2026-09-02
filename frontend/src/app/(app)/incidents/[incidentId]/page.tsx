import { IncidentDetail } from "@/components/incidents/incident-detail";
export default async function Page({
  params,
}: {
  params: Promise<{ incidentId: string }>;
}) {
  return <IncidentDetail id={Number((await params).incidentId)} />;
}
