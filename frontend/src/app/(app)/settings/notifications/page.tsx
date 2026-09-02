import { NotificationSettings } from "@/components/settings/notification-settings";
export default function Page() {
  return (
    <div className="stack">
      <header>
        <p className="eyebrow">Settings</p>
        <h1 className="page-title">Notification alerts</h1>
        <p className="muted mt-2">
          Choose where Uptora sends confirmed outage and recovery messages.
        </p>
      </header>
      <NotificationSettings />
    </div>
  );
}
