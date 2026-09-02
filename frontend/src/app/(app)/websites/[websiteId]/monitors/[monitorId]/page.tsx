import { MonitorDetail } from "@/components/monitors/monitor-detail";
export default async function Page({
  params,
}: {
  params: Promise<{ websiteId: string; monitorId: string }>;
}) {
  const p = await params;
  return (
    <MonitorDetail
      websiteId={Number(p.websiteId)}
      monitorId={Number(p.monitorId)}
    />
  );
}
