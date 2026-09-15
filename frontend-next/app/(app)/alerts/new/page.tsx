import { AlertsNewPage } from "@/features/alerts/components/alerts-new-page";

export default async function NewAlertPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  return <AlertsNewPage mode={params.mode ?? null} />;
}
