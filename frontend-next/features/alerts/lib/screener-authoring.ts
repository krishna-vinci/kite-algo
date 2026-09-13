/**
 * Screener authoring model.
 *
 * Mirrors the server's canonical serialization (`ScreenerSpec.to_document_dict`
 * and friends), so a document built here is the SAME shape the parser would
 * store — in particular `freshness_limit_s` in seconds, `schedule.calendar`
 * always present, and rank bands materialized on `top_n` attachments exactly
 * the way the parser defaults them.
 *
 * Everything is pure. The backend remains the authority; this file exists so
 * the editor can explain problems before a round trip, not to replace
 * validation.
 */

import {
  conditionFromDocument,
  conditionToDocument,
  emptyUniverseDraft,
  instrumentKeyFromDocument,
  operandFromDocument,
  refsFromDocument,
  universeDraftIssues,
  type AlertTargeting,
  type Condition,
  type UniverseDraft,
} from "@/features/alerts/lib/authoring";

export const SCREENER_TRIGGERS = ["entry", "exit", "top_n", "rank_delta"] as const;
export type ScreenerTrigger = (typeof SCREENER_TRIGGERS)[number];

/**
 * Ranking can only be a field or an indicator — never a bare constant or an
 * expression. Modelling that as its own type means the editor cannot offer an
 * operand the compiler would reject.
 */
export type RankOperand =
  | { kind: "field"; name: string }
  | { kind: "indicator"; name: string; period?: number };

export type ScreenerAttachmentDraft = {
  id: string;
  trigger: ScreenerTrigger;
  channels: string[];
  top_n: number | null;
  rank_delta: number | null;
  entry_rank: number | null;
  exit_rank: number | null;
  exit_after: number | null;
  initial_match: boolean;
  message: string | null;
};

export type ScreenerDraft = {
  name: string;
  session: string;
  clock: string;
  timeframe: string;
  targeting: AlertTargeting;
  instruments: string[];
  universe: UniverseDraft;
  stageKind: "signal" | "filter";
  conditions: Condition[];
  schedule: { every: string; calendar: string; at: string };
  rank: { by: RankOperand; direction: "desc" | "asc" };
  top_n: number;
  freshness_limit_s: number;
  attachments: ScreenerAttachmentDraft[];
};

/**
 * The parser's own default rank band for `top_n` attachments:
 * `entry_rank = top_n`, `exit_rank = top_n + max(1, top_n // 2)`.
 *
 * Duplicated here deliberately so the form shows the band the server will
 * actually enforce instead of an empty box that implies "no hysteresis".
 */
export function defaultRankBand(topN: number): { entry_rank: number; exit_rank: number } {
  const bounded = Math.max(1, Math.min(1000, Math.trunc(topN) || 1));
  return { entry_rank: bounded, exit_rank: bounded + Math.max(1, Math.floor(bounded / 2)) };
}

/**
 * The backend's supported schedule range is 5m..31d (`_schedule_spec`). The
 * list stops at 31d deliberately — offering a longer interval would produce a
 * document the server rejects.
 */
export const DURATION_CHOICES = [
  { label: "5 minutes", value: "5m" },
  { label: "15 minutes", value: "15m" },
  { label: "30 minutes", value: "30m" },
  { label: "1 hour", value: "1h" },
  { label: "4 hours", value: "4h" },
  { label: "1 day", value: "1d" },
  { label: "1 week", value: "7d" },
  { label: "2 weeks", value: "14d" },
  { label: "31 days", value: "31d" },
] as const;

/**
 * The stored canonical `every` is a seconds string (e.g. `"86400s"`). Render it
 * back to a friendly unit when it divides cleanly, so the edit path can select
 * the value it loaded instead of showing an empty box.
 */
export function durationFromSeconds(every: unknown): string {
  if (typeof every !== "string") return "1d";
  const match = /^(\d+)s$/.exec(every.trim());
  if (!match) return every;
  const seconds = Number(match[1]);
  for (const [unit, divisor] of [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
  ] as const) {
    if (seconds % divisor === 0 && seconds >= divisor) return `${seconds / divisor}${unit}`;
  }
  return `${seconds}s`;
}

export const FRESHNESS_CHOICES = [
  { label: "5 minutes", seconds: 300 },
  { label: "1 hour", seconds: 3600 },
  { label: "1 day", seconds: 86400 },
  { label: "3 days", seconds: 3 * 24 * 3600 },
  { label: "7 days", seconds: 7 * 24 * 3600 },
  { label: "30 days", seconds: 30 * 24 * 3600 },
] as const;

export function emptyAttachment(index: number): ScreenerAttachmentDraft {
  return {
    id: `attachment-${index + 1}`,
    trigger: "entry",
    channels: [],
    top_n: null,
    rank_delta: null,
    entry_rank: null,
    exit_rank: null,
    exit_after: null,
    initial_match: false,
    message: null,
  };
}

export function emptyScreenerDraft(): ScreenerDraft {
  return {
    name: "",
    session: "nse_equity",
    clock: "candle_close",
    timeframe: "day",
    targeting: "universe",
    instruments: [],
    universe: emptyUniverseDraft(),
    stageKind: "signal",
    conditions: [
      {
        left: { kind: "field", name: "change_pct" },
        op: "gt",
        right: { kind: "constant", value: 2 },
      },
    ],
    schedule: { every: "1d", calendar: "nse_equity", at: "session_close" },
    rank: { by: { kind: "field", name: "change_pct" }, direction: "desc" },
    top_n: 20,
    freshness_limit_s: 3 * 24 * 3600,
    attachments: [],
  };
}

function attachmentToDocument(attachment: ScreenerAttachmentDraft): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    id: attachment.id,
    trigger: attachment.trigger,
    channels: attachment.channels,
    initial_match: attachment.initial_match,
  };
  const numericKeys = ["top_n", "rank_delta", "entry_rank", "exit_rank", "exit_after"] as const;
  for (const key of numericKeys) {
    const value = attachment[key];
    if (value !== null) payload[key] = value;
  }
  if (attachment.message !== null && attachment.message !== "") payload.message = attachment.message;
  return payload;
}

function screenerBlock(draft: ScreenerDraft): Record<string, unknown> {
  const screener: Record<string, unknown> = {
    schedule: {
      every: draft.schedule.every,
      calendar: draft.schedule.calendar,
      ...(draft.schedule.at ? { at: draft.schedule.at } : {}),
    },
    rank: {
      by:
        draft.rank.by.kind === "field"
          ? { field: draft.rank.by.name }
          : { indicator: draft.rank.by.name },
      direction: draft.rank.direction,
    },
    top_n: draft.top_n,
    freshness_limit_s: draft.freshness_limit_s,
  };
  if (draft.attachments.length > 0) {
    screener.attachments = draft.attachments.map(attachmentToDocument);
  }
  return screener;
}

/**
 * Build the screener document.
 *
 * With a `base` (the edit path) the modeled fields are merged onto a deep clone
 * of the loaded document, so keys this editor does not model survive. Without a
 * base the canonical skeleton is emitted.
 */
export function buildScreenerDocument(
  draft: ScreenerDraft,
  base?: Record<string, unknown> | null,
): Record<string, unknown> {
  const useUniverse =
    draft.targeting === "universe" && draft.universe.union.some((ref) => ref.name.trim() !== "");

  if (!base) {
    return {
      version: 1,
      name: draft.name,
      session: draft.session,
      instruments: useUniverse ? [] : draft.instruments,
      universe: useUniverse ? universeRefsToDocument(draft.universe) : null,
      stages: [
        {
          id: "scan",
          type: draft.stageKind,
          clock: draft.clock,
          timeframe: draft.timeframe,
          conditions: { all: draft.conditions.map(conditionToDocument) },
        },
      ],
      screener: screenerBlock(draft),
    };
  }

  const document = JSON.parse(JSON.stringify(base)) as Record<string, unknown>;
  document.name = draft.name;
  document.session = draft.session;
  document.instruments = useUniverse ? [] : draft.instruments;
  document.universe = useUniverse ? universeRefsToDocument(draft.universe) : null;

  const stages = Array.isArray(document.stages)
    ? (document.stages as Array<Record<string, unknown>>)
    : [];
  const stage = stages[0] ?? { id: "scan" };
  stage.type = draft.stageKind;
  stage.clock = draft.clock;
  stage.timeframe = draft.timeframe;
  stage.conditions = { all: draft.conditions.map(conditionToDocument) };
  if (stages.length === 0) {
    document.stages = [stage];
  } else {
    stages[0] = stage;
    document.stages = stages;
  }

  document.screener = { ...(document.screener as Record<string, unknown> | undefined), ...screenerBlock(draft) };
  return document;
}

function universeRefsToDocument(universe: UniverseDraft): Record<string, unknown> {
  const document: Record<string, unknown> = {
    union: universe.union
      .filter((ref) => ref.name.trim() !== "")
      .map((ref) => ({ kind: ref.kind, name: ref.name })),
  };
  const exclude = universe.exclude
    .filter((ref) => ref.name.trim() !== "")
    .map((ref) => ({ kind: ref.kind, name: ref.name }));
  if (exclude.length > 0) document.exclude = exclude;
  document.deduplicate = universe.deduplicate;
  return document;
}

// ---------------------------------------------------------------------------
// client-side checks (the server stays the authority)
// ---------------------------------------------------------------------------

export function screenerDraftIssues(draft: ScreenerDraft): string[] {
  const issues: string[] = [];

  if (draft.targeting === "universe") {
    issues.push(...universeDraftIssues(draft.universe));
  } else if (draft.instruments.length === 0) {
    issues.push("A screener needs instruments or a universe to scan.");
  }

  if (draft.conditions.length === 0) {
    issues.push("A screener needs at least one condition to decide who qualifies.");
  }

  if (draft.top_n < 1 || draft.top_n > 1000) {
    issues.push("top_n must be between 1 and 1000.");
  }

  if (draft.freshness_limit_s < 300 || draft.freshness_limit_s > 30 * 24 * 3600) {
    issues.push("The freshness limit must be between 5 minutes and 30 days.");
  }

  if (draft.schedule.every === "1d" && !draft.schedule.at) {
    issues.push("A daily scan needs a time — 'HH:MM' IST or 'session_close'.");
  }

  const seen = new Set<string>();
  for (const attachment of draft.attachments) {
    if (attachment.id.trim() === "") {
      issues.push("Every attachment needs an id.");
    } else if (seen.has(attachment.id)) {
      issues.push(`Attachment id '${attachment.id}' is used more than once.`);
    }
    seen.add(attachment.id);

    if (attachment.channels.length === 0) {
      issues.push(`Attachment '${attachment.id}' has no notification channel, so it could never notify.`);
    }
    if (attachment.trigger === "top_n" && attachment.top_n === null) {
      issues.push(`Attachment '${attachment.id}' uses the top_n trigger, which needs its own top_n.`);
    }
    if (attachment.trigger === "rank_delta" && attachment.rank_delta === null) {
      issues.push(`Attachment '${attachment.id}' uses rank_delta, which needs a rank_delta threshold.`);
    }
    if (
      attachment.entry_rank !== null &&
      attachment.exit_rank !== null &&
      attachment.exit_rank <= attachment.entry_rank
    ) {
      // The parser rejects this, and it is the E-17 oscillation guard: an exit
      // band at or below the entry band makes a member flap on every scan.
      issues.push(
        `Attachment '${attachment.id}': exit_rank must be greater than entry_rank, otherwise a member oscillates in and out on every scan.`,
      );
    }
  }

  return issues;
}

/** Which hysteresis controls are meaningful for a trigger. */
export function attachmentSupportsRankBands(trigger: ScreenerTrigger): boolean {
  return trigger === "top_n" || trigger === "entry" || trigger === "exit";
}

// ---------------------------------------------------------------------------
// document -> draft (the edit path)
// ---------------------------------------------------------------------------

export type ScreenerDraftFromDocument =
  | { ok: true; draft: ScreenerDraft }
  | { ok: false; reason: string };

function triggerOf(raw: unknown): ScreenerTrigger | null {
  return SCREENER_TRIGGERS.includes(raw as ScreenerTrigger) ? (raw as ScreenerTrigger) : null;
}

/**
 * Rebuild an editable screener draft, or explain why it cannot be edited here.
 *
 * Same contract as the alert editor: refuse rather than silently drop. A
 * screener carrying several scan stages or a hand-written arithmetic rank
 * operand is still perfectly valid — it just is not representable in these
 * controls, and opening the form would destroy it on save.
 */
export function documentToScreenerDraft(
  document: Record<string, unknown> | null,
): ScreenerDraftFromDocument {
  if (!document) return { ok: false, reason: "No definition is stored for this screener." };

  const screener = document.screener as Record<string, unknown> | undefined;
  if (!screener) {
    return { ok: false, reason: "This workflow has no screener block; use the alert editor." };
  }

  const stages = Array.isArray(document.stages) ? document.stages : [];
  if (stages.length !== 1) {
    return {
      ok: false,
      reason: `This screener has ${stages.length} stages; this editor models exactly one scan stage.`,
    };
  }
  const stage = stages[0] as Record<string, unknown>;
  // Canonical documents carry empty `any_conditions`/`not_conditions` arrays even
  // when unused, so only a POPULATED group is outside this editor.
  const anyRaw = Array.isArray(stage.any_conditions) ? stage.any_conditions : [];
  const notRaw = Array.isArray(stage.not_conditions) ? stage.not_conditions : [];
  if (stage.sequence || stage.breadth || stage.consecutive_bars || anyRaw.length || notRaw.length) {
    return {
      ok: false,
      reason: "This scan stage uses advanced conditions the structured editor does not model.",
    };
  }
  if (Array.isArray(document.alerts) && document.alerts.length > 0) {
    return {
      ok: false,
      reason: "This screener declares alerts, which a screener document must not (attachments notify instead).",
    };
  }

  // Canonical conditions are a list; a form-shaped fixture may wrap them in
  // `{all: [...]}`. Accept both, as the alert editor does.
  const stageConditions = stage.conditions as unknown;
  let allConditions: unknown[] | null = null;
  if (Array.isArray(stageConditions)) {
    allConditions = stageConditions;
  } else if (stageConditions && typeof stageConditions === "object") {
    const groups = stageConditions as Record<string, unknown>;
    if (Array.isArray(groups.all)) allConditions = groups.all;
  }
  if (allConditions === null) {
    return { ok: false, reason: "Conditions must be a single 'all' group for this editor." };
  }

  const conditions: Condition[] = [];
  for (const raw of allConditions) {
    const parsed = conditionFromDocument(raw);
    if (!parsed.ok) return { ok: false, reason: parsed.reason };
    conditions.push(parsed.condition);
  }

  const schedule = (screener.schedule ?? {}) as Record<string, unknown>;
  const rank = (screener.rank ?? {}) as Record<string, unknown>;
  const rankBy = operandFromDocument(rank.by);
  if (!rankBy || (rankBy.kind !== "field" && rankBy.kind !== "indicator")) {
    return { ok: false, reason: "This screener ranks by an expression this editor does not model." };
  }

  const rawUniverse =
    typeof document.universe === "object" && document.universe !== null
      ? (document.universe as Record<string, unknown>)
      : null;
  if (rawUniverse && rawUniverse.intersect) {
    return { ok: false, reason: "This screener intersects universe references, which this editor does not model." };
  }

  const attachments: ScreenerAttachmentDraft[] = [];
  for (const raw of Array.isArray(screener.attachments) ? screener.attachments : []) {
    if (typeof raw !== "object" || raw === null) continue;
    const record = raw as Record<string, unknown>;
    const trigger = triggerOf(record.trigger);
    if (!trigger) {
      return { ok: false, reason: `Unknown attachment trigger '${String(record.trigger)}'.` };
    }
    attachments.push({
      id: String(record.id ?? ""),
      trigger,
      channels: Array.isArray(record.channels) ? record.channels.map(String) : [],
      top_n: numberOrNull(record.top_n),
      rank_delta: numberOrNull(record.rank_delta),
      entry_rank: numberOrNull(record.entry_rank),
      exit_rank: numberOrNull(record.exit_rank),
      exit_after: numberOrNull(record.exit_after),
      initial_match: Boolean(record.initial_match),
      message: typeof record.message === "string" ? record.message : null,
    });
  }

  const union = rawUniverse ? refsFromDocument(rawUniverse.union ?? rawUniverse.refs) : [];
  const targeting: AlertTargeting = rawUniverse && union.length > 0 ? "universe" : "instruments";
  const storedFreshness = numberOrNull(screener.freshness_limit_s);

  return {
    ok: true,
    draft: {
      name: String(document.name ?? ""),
      session: String(document.session ?? ""),
      clock: String(stage.clock ?? "candle_close"),
      timeframe: String(stage.timeframe ?? "day"),
      targeting,
      instruments: Array.isArray(document.instruments)
        ? document.instruments.map(instrumentKeyFromDocument).filter((key) => key !== "")
        : [],
      universe: {
        union: union.length > 0 ? union : emptyUniverseDraft().union,
        exclude: rawUniverse ? refsFromDocument(rawUniverse.exclude) : [],
        deduplicate: rawUniverse ? rawUniverse.deduplicate !== false : true,
      },
      stageKind: stage.type === "filter" ? "filter" : "signal",
      conditions: conditions.length > 0 ? conditions : emptyScreenerDraft().conditions,
      schedule: {
        every: durationFromSeconds(schedule.every),
        calendar: String(schedule.calendar ?? "nse_equity"),
        at: typeof schedule.at === "string" ? schedule.at : "session_close",
      },
      rank: {
        by: rankBy,
        direction: rank.direction === "asc" ? "asc" : "desc",
      },
      top_n: numberOrNull(screener.top_n) ?? 20,
      freshness_limit_s: storedFreshness ?? 3 * 24 * 3600,
      attachments,
    },
  };
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
