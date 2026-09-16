"use client";

import { useQuery } from "@tanstack/react-query";
import { SearchIcon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { searchAlertsInstruments } from "@/features/alerts/api";
import { exchangeOf } from "@/features/alerts/lib/authoring";
import { cn } from "@/lib/utils";

type InstrumentPickerProps = Readonly<{
  selected: string[];
  onChange: (next: string[]) => void;
  /** Exchanges the chosen session accepts; others are flagged as incompatible. */
  acceptedExchanges: string[] | null;
}>;

/**
 * Catalog-backed instrument search.
 *
 * Uses the operator `/instruments/search` route rather than the legacy app
 * search because workflows are keyed by the catalog's `EXCHANGE:SYMBOL`
 * identity — a search that cannot return that would make the operator guess at
 * the one thing that must be exact.
 */
export function InstrumentPicker({ selected, onChange, acceptedExchanges }: InstrumentPickerProps) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const handle = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(handle);
  }, [query]);

  const searchQuery = useQuery({
    queryKey: ["alerts", "instrument-search", debounced],
    queryFn: () => searchAlertsInstruments({ q: debounced, limit: 20 }),
    enabled: debounced.length >= 1,
    staleTime: 60_000,
  });

  const results = searchQuery.data?.results ?? [];

  const toggle = (publicKey: string) => {
    onChange(
      selected.includes(publicKey)
        ? selected.filter((key) => key !== publicKey)
        : [...selected, publicKey],
    );
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="relative">
        <SearchIcon className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search symbol or company name"
          aria-label="Search instruments"
          className="pl-9"
        />
      </div>

      {selected.length > 0 ? (
        <ul className="flex flex-wrap gap-2" aria-label="Selected instruments">
          {selected.map((key) => {
            const compatible =
              acceptedExchanges === null || acceptedExchanges.includes(exchangeOf(key));
            return (
              <li key={key}>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs",
                    compatible
                      ? "border-border/70 bg-background/60"
                      : "border-rose-400/40 bg-rose-400/10 text-rose-300",
                  )}
                >
                  <span className="font-mono">{key}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${key}`}
                    onClick={() => toggle(key)}
                    className="rounded-full p-0.5 hover:bg-white/10"
                  >
                    <XIcon className="size-3" />
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}

      {debounced.length >= 1 ? (
        <div className="max-h-64 overflow-auto rounded-lg border border-border/60">
          {searchQuery.isLoading ? (
            <p className="p-3 text-sm text-muted-foreground">Searching…</p>
          ) : searchQuery.error ? (
            <p className="p-3 text-sm text-rose-300">
              {searchQuery.error instanceof Error ? searchQuery.error.message : "Search failed"}
            </p>
          ) : results.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">No matches.</p>
          ) : (
            <ul className="divide-y divide-border/50">
              {results.map((result) => {
                const compatible =
                  acceptedExchanges === null || acceptedExchanges.includes(result.exchange);
                const isSelected = selected.includes(result.public_key);
                return (
                  <li key={result.public_key}>
                    <button
                      type="button"
                      onClick={() => toggle(result.public_key)}
                      aria-pressed={isSelected}
                      className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-white/5"
                    >
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-sm">{result.public_key}</span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {result.name ?? result.symbol}
                          {result.expiry ? ` · exp ${result.expiry}` : ""}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        {!compatible ? <Badge variant="destructive">wrong session</Badge> : null}
                        {isSelected ? <Badge variant="secondary">selected</Badge> : null}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
