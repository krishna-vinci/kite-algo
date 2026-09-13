/**
 * Authoring model and pure helpers for the structured alert editor.
 *
 * The editor produces a canonical document; it does not invent a second format.
 * `buildDocument` emits exactly the skeleton the SDK and the YAML renderer
 * agree on, so the structured editor, the YAML tab and the canvas are three
 * views of one definition (handoff §11).
 *
 * Everything here is pure and capability-driven: operator groups, bounds and
 * the session/instrument rule all come from `GET /capabilities`, never from a
 * hard-coded copy.
 */

import type { AlertsCapabilities } from "@/features/alerts/types";

// ---------------------------------------------------------------------------
// operands and conditions
// ---------------------------------------------------------------------------

export type OperandKind = "constant" | "field" | "indicator";

export type Operand =
  | { kind: "constant"; value: number }
  | { kind: "field"; name: string }
  | { kind: "indicator"; name: string; period?: number };

export type Condition = {
  left: Operand;
  op: string;
  right: Operand;
};

/** The document's shorthand operand form (handoff §11). */
export function operandToDocument(operand: Operand): unknown {
  switch (operand.kind) {
    case "constant":
      return operand.value;
    case "field":
      return { field: operand.name };
    case "indicator":
      return operand.period === undefined
        ? { indicator: operand.name }
        : { indicator: operand.name, period: operand.period };
  }
}

export function conditionToDocument(condition: Condition): Record<string, unknown> {
  return {
    left: operandToDocument(condition.left),
    op: condition.op,
    right: operandToDocument(condition.right),
  };
}

// ---------------------------------------------------------------------------
// operator groups — the level-vs-crossing fix (handoff §7)
// ---------------------------------------------------------------------------

export type OperatorGroup = "level" | "crossing" | "pct" | "break_" | "range" | "other";

export function operatorGroup(
  op: string,
  operators: AlertsCapabilities["operators"],
): OperatorGroup {
  const group = operators[op];
  if (group === "level" || group === "crossing" || group === "pct" || group === "break_" || group === "range") {
    return group;
  }
  return "other";
}

/**
 * Plain-language labels. The point is to stop an operator choosing "is above /
 * is below" (which reports current truth and never *becomes* true) while
 * expecting a transition notification.
 */
export const OPERATOR_LABELS: Record<string, string> = {
  gt: "is above a level",
  gte: "is at or above a level",
  lt: "is below a level",
  lte: "is at or below a level",
  crosses_above: "crosses above",
  crosses_below: "crosses below",
  rises_pct: "rises by %",
  falls_pct: "falls by %",
  breaks_prev_high: "breaks the previous day high",
  breaks_prev_low: "breaks the previous day low",
  within: "is within a range",
};

export function operatorLabel(op: string): string {
  return OPERATOR_LABELS[op] ?? op;
}

/**
 * True when every condition uses a level operator, i.e. nothing can *transition*.
 *
 * This mirrors the backend's `level_only_never_fires` warning so the UI can
 * explain the problem inline while the operator is still editing. The server's
 * warning remains the authority — this only saves a round trip, and the wizard
 * always surfaces the server's issues too.
 */
export function isLevelOnly(
  conditions: Condition[],
  operators: AlertsCapabilities["operators"],
): boolean {
  if (conditions.length === 0) return false;
  return conditions.every((condition) => operatorGroup(condition.op, operators) === "level");
}

/** Triggers whose notification requires a transition, so a level-only rule cannot emit. */
const TRANSITION_TRIGGERS = new Set(["on_transition", "once", "once_per_session", "reminder"]);

export function triggerRequiresTransition(trigger: string): boolean {
  return TRANSITION_TRIGGERS.has(trigger);
}

/**
 * The inline warning the wizard shows. Returns null when there is nothing to
 * warn about. `reminder` is called out because it *can* emit without a
 * transition, so the advice differs.
 */
export function levelOnlyWarning(
  conditions: Condition[],
  trigger: string,
  operators: AlertsCapabilities["operators"],
): string | null {
  if (!isLevelOnly(conditions, operators)) return null;
  if (!triggerRequiresTransition(trigger)) return null;
  if (trigger === "reminder") {
    return "Every condition is a level test, which reports whether something is currently true and never reports a change. A reminder can still fire on its interval, but if you expect a notification when the level is crossed, choose a crossing operator.";
  }
  return "Every condition is a level test, which reports whether something is currently true and never reports a change. With this trigger the alert will never notify. Choose a crossing operator (crosses above / crosses below) or switch to the reminder trigger.";
}

// ---------------------------------------------------------------------------
// session / instrument compatibility (the compiler's own rule)
// ---------------------------------------------------------------------------

/**
 * Mirrors `registry.session_accepts_exchange`. Case-insensitive on the exchange,
 * exactly like the compiler, so the client check cannot disagree with it.
 */
export function sessionAcceptsExchange(
  session: string,
  exchange: string,
  sessionExchanges: AlertsCapabilities["session_exchanges"],
): boolean {
  const accepted = sessionExchanges[session] ?? [];
  return accepted.includes(exchange.toUpperCase());
}

/** Exchange of a qualified `EXCHANGE:SYMBOL` key. */
export function exchangeOf(instrumentKey: string): string {
  const separator = instrumentKey.indexOf(":");
  return separator === -1 ? "" : instrumentKey.slice(0, separator).toUpperCase();
}

/**
 * One instrument as a qualified `EXCHANGE:SYMBOL` key.
 *
 * The canonical stored form is `{symbol, exchange}` (what `to_document_dict`
 * emits); the authoring shorthand is the bare string. Both must be read, or the
 * editor would show `[object Object]` for every real instrument.
 */
export function instrumentKeyFromDocument(raw: unknown): string {
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object") {
    const record = raw as Record<string, unknown>;
    if (record.symbol && record.exchange) {
      return `${String(record.exchange).toUpperCase()}:${String(record.symbol)}`;
    }
    if (typeof record.public_key === "string") return record.public_key;
    if (typeof record.key === "string") return record.key;
  }
  return "";
}

export type IncompatibleInstrument = { instrumentKey: string; exchange: string };

/** Instruments a session cannot carry. Empty means the pair is valid. */
export function incompatibleInstruments(
  session: string,
  instrumentKeys: string[],
  sessionExchanges: AlertsCapabilities["session_exchanges"],
): IncompatibleInstrument[] {
  return instrumentKeys
    .filter((key) => !sessionAcceptsExchange(session, exchangeOf(key), sessionExchanges))
    .map((key) => ({ instrumentKey: key, exchange: exchangeOf(key) }));
}

// ---------------------------------------------------------------------------
// universe targeting
// ---------------------------------------------------------------------------

export type UniverseRefKind = "universe" | "index" | "watchlist";

export type UniverseRefDraft = { kind: UniverseRefKind; name: string };

export type UniverseDraft = {
  union: UniverseRefDraft[];
  exclude: UniverseRefDraft[];
  deduplicate: boolean;
};

/**
 * The typed reference form. The parser also accepts the shorthand
 * (`{universe: name}`), but exactly one form is emitted so a document written
 * here and one written by the SDK normalise identically.
 */
export function universeRefToDocument(ref: UniverseRefDraft): Record<string, unknown> {
  return { kind: ref.kind, name: ref.name };
}

export function emptyUniverseDraft(): UniverseDraft {
  return { union: [{ kind: "universe", name: "" }], exclude: [], deduplicate: true };
}

function isRefKind(value: unknown): value is UniverseRefKind {
  return value === "universe" || value === "index" || value === "watchlist";
}

/** Parse either the typed or the shorthand reference form. */
export function universeRefFromDocument(raw: unknown): UniverseRefDraft | null {
  if (typeof raw !== "object" || raw === null) return null;
  const record = raw as Record<string, unknown>;
  if (typeof record.kind === "string" && isRefKind(record.kind)) {
    return { kind: record.kind, name: String(record.name ?? "") };
  }
  for (const kind of ["universe", "index", "watchlist"] as const) {
    if (kind in record) return { kind, name: String(record[kind] ?? "") };
  }
  return null;
}

export function refsFromDocument(raw: unknown): UniverseRefDraft[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(universeRefFromDocument).filter((ref): ref is UniverseRefDraft => ref !== null);
}

function universeToDocument(universe: UniverseDraft): Record<string, unknown> {
  const document: Record<string, unknown> = {
    union: universe.union
      .filter((ref) => ref.name.trim() !== "")
      .map(universeRefToDocument),
  };
  const exclude = universe.exclude
    .filter((ref) => ref.name.trim() !== "")
    .map(universeRefToDocument);
  if (exclude.length > 0) document.exclude = exclude;
  document.deduplicate = universe.deduplicate;
  return document;
}

/**
 * Client-side universe checks.
 *
 * Only the rules the UI can decide without the server are duplicated here; the
 * compiler stays the authority and its issues are always shown too.
 */
export function universeDraftIssues(universe: UniverseDraft): string[] {
  const issues: string[] = [];
  const named = universe.union.filter((ref) => ref.name.trim() !== "");
  if (named.length === 0) {
    issues.push("A universe expression needs at least one reference.");
  }
  if (universe.union.some((ref) => ref.name.trim() === "")) {
    issues.push("Every universe reference needs a name — an empty one is not a wildcard.");
  }
  return issues;
}

// ---------------------------------------------------------------------------
// draft -> document
// ---------------------------------------------------------------------------

export type AlertTriggerDraft = {
  id: string;
  trigger: string;
  channels: string[];
  cooldown_s: number | null;
  rearm_level: number | null;
  rearm_direction: string | null;
  reminder_interval_s: number | null;
  notify_if_already_true: boolean;
  max_per_session: number | null;
};

export type AlertTargeting = "instruments" | "universe";

export type AlertDraft = {
  name: string;
  session: string;
  clock: string;
  timeframe: string;
  targeting: AlertTargeting;
  instruments: string[];
  universe: UniverseDraft;
  conditions: Condition[];
  /** The stage the edited conditions live on. Captured so an edit through the
   * form cannot silently rename a stage (`px` -> whatever) and move the hash. */
  stageId: string;
  alert: AlertTriggerDraft;
};

export function emptyDraft(): AlertDraft {
  return {
    name: "",
    session: "",
    clock: "candle_close",
    timeframe: "15minute",
    targeting: "instruments",
    instruments: [],
    universe: emptyUniverseDraft(),
    conditions: [{ left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 0 } }],
    stageId: "px",
    alert: {
      id: "a1",
      trigger: "on_transition",
      channels: [],
      cooldown_s: null,
      rearm_level: null,
      rearm_direction: null,
      reminder_interval_s: null,
      notify_if_already_true: false,
      max_per_session: null,
    },
  };
}

function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** Optional alert keys are emitted only when set, and cleared when unset, so an
 * untouched field never moves the hash and clearing one actually removes it. */
function applyOptional(target: Record<string, unknown>, key: string, value: unknown): void {
  if (value === null || value === undefined) delete target[key];
  else target[key] = value;
}

function alertToDocument(alert: AlertTriggerDraft, stageId: string): Record<string, unknown> {
  const document: Record<string, unknown> = {
    id: alert.id,
    source: stageId,
    trigger: alert.trigger,
    channels: alert.channels,
  };
  applyOptional(document, "cooldown_s", alert.cooldown_s);
  applyOptional(document, "rearm_level", alert.rearm_level);
  applyOptional(document, "rearm_direction", alert.rearm_direction);
  applyOptional(document, "reminder_interval_s", alert.reminder_interval_s);
  applyOptional(document, "max_per_session", alert.max_per_session);
  if (alert.notify_if_already_true) document.notify_if_already_true = true;
  return document;
}

function stageToDocument(draft: AlertDraft): Record<string, unknown> {
  return {
    id: draft.stageId || "px",
    type: "signal",
    clock: draft.clock,
    timeframe: draft.timeframe,
    conditions: { all: draft.conditions.map(conditionToDocument) },
  };
}

/**
 * Build the canonical document from the draft.
 *
 * When `base` is provided (the edit path) the modeled fields are merged ONTO a
 * deep clone of the loaded document rather than reconstructed from the draft.
 * That is what makes the form lossless: an `expires_at`, a `message`, a
 * `session_cap_reset`, indicator `source`/`offset` attributes, or any key this
 * editor does not model survives a save untouched. Rebuilding from the partial
 * model would silently delete them — and a no-op save would change the hash,
 * which is exactly the failure the handoff warns about.
 *
 * With no base (the create path) there is nothing to preserve and the canonical
 * skeleton is emitted.
 */
export function buildDocument(
  draft: AlertDraft,
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
      universe: useUniverse ? universeToDocument(draft.universe) : null,
      stages: [stageToDocument(draft)],
      alerts: [alertToDocument(draft.alert, draft.stageId || "px")],
    };
  }

  const document = deepClone(base);
  document.version = typeof base.version === "number" ? base.version : 1;
  document.name = draft.name;
  document.session = draft.session;
  document.instruments = useUniverse ? [] : draft.instruments;
  document.universe = useUniverse ? universeToDocument(draft.universe) : null;

  const stages = Array.isArray(document.stages)
    ? (document.stages as Array<Record<string, unknown>>)
    : [];
  const stageId = draft.stageId || String(stages[0]?.id ?? "px");
  let stage = stages.find((candidate) => candidate.id === stageId);
  if (!stage) {
    stage = { id: stageId, type: "signal" };
    stages.push(stage);
  }
  document.stages = stages;
  stage.clock = draft.clock;
  stage.timeframe = draft.timeframe;
  stage.conditions = { all: draft.conditions.map(conditionToDocument) };

  const alerts = Array.isArray(document.alerts)
    ? (document.alerts as Array<Record<string, unknown>>)
    : [];
  let alert = alerts.find((candidate) => candidate.id === draft.alert.id);
  if (!alert) {
    alert = { id: draft.alert.id };
    alerts.push(alert);
  }
  document.alerts = alerts;
  alert.source = stageId;
  alert.trigger = draft.alert.trigger;
  alert.channels = draft.alert.channels;
  applyOptional(alert, "cooldown_s", draft.alert.cooldown_s);
  applyOptional(alert, "rearm_level", draft.alert.rearm_level);
  applyOptional(alert, "rearm_direction", draft.alert.rearm_direction);
  applyOptional(alert, "reminder_interval_s", draft.alert.reminder_interval_s);
  applyOptional(alert, "max_per_session", draft.alert.max_per_session);
  if (draft.alert.notify_if_already_true) alert.notify_if_already_true = true;
  else delete alert.notify_if_already_true;

  return document;
}

// ---------------------------------------------------------------------------
// document -> draft (the edit path)
// ---------------------------------------------------------------------------

export type DraftFromDocument =
  | { ok: true; draft: AlertDraft }
  | { ok: false; reason: string };

/** Operand attributes this editor does not model; any of them means "not editable here". */
function operandHasUnmodeledAttributes(record: Record<string, unknown>): boolean {
  if (record.source != null || record.offset != null) return true;
  const params = record.params;
  return Boolean(params && typeof params === "object" && Object.keys(params as object).length > 0);
}

export function operandFromDocument(raw: unknown): Operand | null {
  if (typeof raw === "number") return { kind: "constant", value: raw };
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;

  // Authoring shorthand.
  if (!("kind" in record) && "field" in record) return { kind: "field", name: String(record.field) };
  if (!("kind" in record) && "indicator" in record) {
    const period = record.period;
    return {
      kind: "indicator",
      name: String(record.indicator),
      ...(typeof period === "number" ? { period } : {}),
    };
  }

  // Serialized canonical form: {kind, name, value, params, source, offset}.
  // This is what the backend actually STORES, so it must be handled or the
  // structured editor would be unable to open a real document.
  if (record.kind === "field") {
    if (operandHasUnmodeledAttributes(record)) return null;
    return { kind: "field", name: String(record.name ?? "") };
  }
  if (record.kind === "value" && typeof record.value === "number") {
    if (operandHasUnmodeledAttributes(record)) return null;
    return { kind: "constant", value: record.value };
  }
  if (record.kind === "indicator") {
    if (record.source != null || record.offset != null) return null;
    const params = (record.params ?? {}) as Record<string, unknown>;
    const extra = Object.keys(params).filter((key) => key !== "period");
    if (extra.length > 0) return null;
    const operand: Operand = { kind: "indicator", name: String(record.name ?? "") };
    if (typeof params.period === "number") operand.period = params.period;
    return operand;
  }
  // Pair operands, arithmetic expressions, external operands and anything else
  // are outside what the structured editor models.
  return null;
}

/**
 * Parse one condition, returning a REASON when it cannot be represented.
 *
 * Shared by the alert and screener editors so both refuse the same constructs
 * with the same wording, and so the two cannot drift apart.
 */
export function conditionFromDocument(
  raw: unknown,
): { ok: true; condition: Condition } | { ok: false; reason: string } {
  if (typeof raw !== "object" || raw === null) {
    return { ok: false, reason: "A condition is not in a form this editor understands." };
  }
  const record = raw as Record<string, unknown>;
  if (record.hysteresis) {
    return { ok: false, reason: "This condition uses hysteresis, which this editor does not model yet." };
  }
  const left = operandFromDocument(record.left);
  const right = operandFromDocument(record.right);
  if (!left || !right) {
    return {
      ok: false,
      reason: "This condition uses a pair or expression operand, which this editor does not model yet.",
    };
  }
  return { ok: true, condition: { left, op: String(record.op ?? ""), right } };
}

/**
 * Rebuild an editable draft from a stored document.
 *
 * Returns a REASON when the definition uses anything the structured editor does
 * not model (sequences, breadth, universes, several stages or alerts, `any`/
 * `not` groups, pair operands, arithmetic). The caller must then offer the
 * read-only/YAML view rather than open a form that would silently drop fields
 * on save — losing a `sequence` because the form never showed it would be a
 * data-destroying "success".
 */
export function documentToDraft(document: Record<string, unknown> | null): DraftFromDocument {
  if (!document) return { ok: false, reason: "No definition is stored for this workflow." };

  if (document.screener) {
    return { ok: false, reason: "This is a screener; use the screener editor." };
  }

  const stages = Array.isArray(document.stages) ? document.stages : [];
  if (stages.length !== 1) {
    return { ok: false, reason: `This alert has ${stages.length} stages; this editor models exactly one.` };
  }
  const stage = stages[0] as Record<string, unknown>;
  if (stage.type !== "signal") {
    return { ok: false, reason: `Stage type '${String(stage.type)}' is not editable here.` };
  }
  if (stage.sequence || stage.breadth || stage.consecutive_bars) {
    return {
      ok: false,
      reason:
        "This stage uses advanced conditions (sequence, breadth or consecutive_bars) that the structured editor does not model.",
    };
  }
  // Canonical documents carry these as empty arrays even when unused, so only a
  // POPULATED group is outside the editor.
  const anyConditions = Array.isArray(stage.any_conditions) ? stage.any_conditions : [];
  const notConditions = Array.isArray(stage.not_conditions) ? stage.not_conditions : [];
  if (anyConditions.length > 0 || notConditions.length > 0) {
    return { ok: false, reason: "This stage uses 'any'/'not' groups, which this editor does not model." };
  }

  // The backend stores `conditions` in the canonical list form
  // (`[{left, op, right}, ...]`), while a form-shaped fixture may wrap it as
  // `{all: [...]}`. Accept both — refusing the canonical form would make the
  // editor unable to open any real document.
  const stageConditions = stage.conditions as unknown;
  let allConditions: unknown[] | null = null;
  if (Array.isArray(stageConditions)) {
    allConditions = stageConditions;
  } else if (stageConditions && typeof stageConditions === "object") {
    const groups = stageConditions as Record<string, unknown>;
    const inlineAny = Array.isArray(groups.any) ? groups.any : [];
    const inlineNot = Array.isArray(groups.not) ? groups.not : [];
    if (inlineAny.length > 0 || inlineNot.length > 0) {
      return { ok: false, reason: "This stage uses 'any'/'not' groups, which this editor does not model." };
    }
    if (Array.isArray(groups.all)) allConditions = groups.all;
  }
  if (allConditions === null) {
    return {
      ok: false,
      reason: "Conditions must be a single 'all' group for this editor to represent them.",
    };
  }

  const conditions: Condition[] = [];
  for (const raw of allConditions) {
    const parsed = conditionFromDocument(raw);
    if (!parsed.ok) return { ok: false, reason: parsed.reason };
    conditions.push(parsed.condition);
  }
  if (conditions.length === 0) {
    return { ok: false, reason: "This stage has no conditions to edit." };
  }

  const alerts = Array.isArray(document.alerts) ? document.alerts : [];
  if (alerts.length !== 1) {
    return { ok: false, reason: `This alert has ${alerts.length} alerts; this editor models exactly one.` };
  }
  const alert = alerts[0] as Record<string, unknown>;

  const rawUniverse =
    typeof document.universe === "object" && document.universe !== null
      ? (document.universe as Record<string, unknown>)
      : null;

  // `intersect` is a real document feature with no editor control here, so it
  // is refused rather than dropped: silently removing a membership restriction
  // would widen the scan on save.
  if (rawUniverse && rawUniverse.intersect) {
    return {
      ok: false,
      reason: "This alert intersects universe references, which this editor does not model yet.",
    };
  }

  const union = rawUniverse ? refsFromDocument(rawUniverse.union ?? rawUniverse.refs) : [];
  const exclude = rawUniverse ? refsFromDocument(rawUniverse.exclude) : [];
  const targeting: AlertTargeting = rawUniverse && union.length > 0 ? "universe" : "instruments";

  const instruments = Array.isArray(document.instruments)
    ? document.instruments.map(instrumentKeyFromDocument).filter((key) => key !== "")
    : [];

  return {
    ok: true,
    draft: {
      name: String(document.name ?? ""),
      session: String(document.session ?? ""),
      clock: stage.clock != null ? String(stage.clock) : "candle_close",
      timeframe: stage.timeframe != null ? String(stage.timeframe) : "",
      targeting,
      instruments,
      universe: {
        union: union.length > 0 ? union : emptyUniverseDraft().union,
        exclude,
        deduplicate: rawUniverse ? rawUniverse.deduplicate !== false : true,
      },
      conditions,
      stageId: String(stage.id ?? "px"),
      alert: {
        id: String(alert.id ?? "a1"),
        trigger: String(alert.trigger ?? "on_transition"),
        channels: Array.isArray(alert.channels) ? alert.channels.map(String) : [],
        cooldown_s: typeof alert.cooldown_s === "number" ? alert.cooldown_s : null,
        rearm_level: typeof alert.rearm_level === "number" ? alert.rearm_level : null,
        rearm_direction:
          alert.rearm_direction === "above" || alert.rearm_direction === "below"
            ? alert.rearm_direction
            : null,
        reminder_interval_s:
          typeof alert.reminder_interval_s === "number" ? alert.reminder_interval_s : null,
        notify_if_already_true: Boolean(alert.notify_if_already_true),
        max_per_session: typeof alert.max_per_session === "number" ? alert.max_per_session : null,
      },
    },
  };
}
