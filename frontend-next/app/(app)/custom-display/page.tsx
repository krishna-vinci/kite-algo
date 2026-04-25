import { PlaceholderPage } from "@/components/placeholder-page";

export default function CustomDisplayPage() {
  return (
    <PlaceholderPage
      title="Custom display lab"
      description="This workspace is reserved for future operator layout composition. Fake KPI tiles and pseudo-live activity have been removed so the page reads clearly as a design lab, not a live market surface."
      planned={[
        "Drag-and-drop panel composition",
        "Saved multi-panel workspace presets",
        "Shared panel registry for trading, journal, and alerts modules",
      ]}
    />
  );
}
