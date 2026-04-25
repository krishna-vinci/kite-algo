import { PlaceholderPage } from "@/components/placeholder-page";

export default function AlertsPage() {
  return (
    <PlaceholderPage
      title="Alerts"
      description="Alert authoring, delivery status, and alert history will live here once the frontend is wired to the canonical alerts API instead of static example rows."
      planned={[
        "Live alert CRUD with broker/runtime-backed conditions",
        "Delivery history and acknowledgement states",
        "Severity-aware notification routing",
      ]}
    />
  );
}
