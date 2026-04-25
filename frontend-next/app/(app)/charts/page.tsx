import { PlaceholderPage } from "@/components/placeholder-page";

export default function ChartsPage() {
  return (
    <PlaceholderPage
      title="Charts"
      description="Charting will return as a dedicated live module once candlestick overlays, timeframe sync, and chart annotations are wired to canonical market data. This placeholder keeps the shell honest until then."
      planned={[
        "Lightweight-charts integration with live candles",
        "Overlay library for VWAP, EMA, and operator annotations",
        "Linked symbol/timeframe state across trading workspaces",
      ]}
    />
  );
}
