import { WebsiteDetail } from "@/components/websites/website-detail";
export default async function Page({
  params,
}: {
  params: Promise<{ websiteId: string }>;
}) {
  return <WebsiteDetail id={(await params).websiteId} />;
}
