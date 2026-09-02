import { MonitorForm } from "@/components/monitors/monitor-form";
export default async function Page({
  params,
}: {
  params: Promise<{ websiteId: string }>;
}) {
  const id = Number((await params).websiteId);
  return (
    <div className="stack">
      <header>
        <p className="eyebrow">New monitor</p>
        <h1 className="page-title">Configure a check</h1>
      </header>
      <MonitorForm websiteId={id} />
    </div>
  );
}
