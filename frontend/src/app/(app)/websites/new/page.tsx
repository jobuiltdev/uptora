import { WebsiteForm } from "@/components/websites/website-form";
export default function Page() {
  return (
    <div className="stack">
      <header>
        <p className="eyebrow">New website</p>
        <h1 className="page-title">Add a website</h1>
      </header>
      <WebsiteForm />
    </div>
  );
}
