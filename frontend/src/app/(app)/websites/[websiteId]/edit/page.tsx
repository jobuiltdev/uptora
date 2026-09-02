import { WebsiteEdit } from "@/components/websites/website-edit";

export default async function Page({
  params,
}: {
  params: Promise<{ websiteId: string }>;
}) {
  return (
    <div className="stack">
      <header>
        <p className="eyebrow">Website settings</p>
        <h1 className="page-title">Edit website</h1>
      </header>
      <WebsiteEdit id={(await params).websiteId} />
    </div>
  );
}
