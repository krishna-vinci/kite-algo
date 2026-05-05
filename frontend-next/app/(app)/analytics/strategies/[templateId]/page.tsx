import { redirect } from "next/navigation";

export default async function AnalyticsStrategyDeepDiveRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ templateId: string }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { templateId } = await params;
  const resolved = (await searchParams) ?? {};
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(resolved)) {
    if (typeof value === "string") sp.set(key, value);
    else if (Array.isArray(value) && value[0]) sp.set(key, value[0]);
  }
  const qs = sp.toString();
  redirect(`/journal/analytics/strategies/${templateId}${qs ? `?${qs}` : ""}`);
}
