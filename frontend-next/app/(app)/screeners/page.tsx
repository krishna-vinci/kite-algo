import { PlaceholderPage } from "@/components/placeholder-page";

export default function ScreenersPage() {
  return (
    <PlaceholderPage
      title="Screeners"
      description="The screener workspace is reserved for a real filter-builder and result stream. Static symbol tables have been removed so this page does not imply live market coverage before the backend contracts exist."
      planned={[
        "Saved screener definitions backed by API state",
        "Realtime result tables with pagination and export",
        "Reusable filter chips and condition groups",
      ]}
    />
  );
}
