"use client";

/**
 * The two small pieces of the composer that are presentation only: the
 * readiness verdict (including the honest "partly verified" state) and the
 * row that adds one ordinary parameter field.
 */

import { useState } from "react";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleAlertIcon,
  Loader2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { SchemaField } from "@/features/strategies/lib/schema";
import type { SourceReadiness } from "@/lib/hosted-strategies/types";

export type ReadinessState =
  | { kind: "idle" }
  | { kind: "checking"; source: string }
  | { kind: "ready"; source: string; result: SourceReadiness }
  | { kind: "unknown"; source: string; result: SourceReadiness; reasons: string[] }
  | { kind: "blocked"; source: string; result: SourceReadiness; reasons: string[] }
  | { kind: "error"; source: string; message: string };

export function ReadinessView({
  state,
  stale,
}: Readonly<{ state: ReadinessState; stale: boolean }>) {
  if (state.kind === "idle") {
    return (
      <p className="text-xs text-muted-foreground">
        Readiness is checked as you type. Nothing is run and nothing is stored.
      </p>
    );
  }
  if (state.kind === "checking" || stale) {
    return (
      <p
        className="flex items-center gap-2 text-xs text-muted-foreground"
        data-testid="readiness-checking"
      >
        <Loader2Icon className="size-3 animate-spin" aria-hidden />
        Checking the current source…
      </p>
    );
  }
  if (state.kind === "error") {
    return (
      <Alert variant="destructive" data-testid="readiness-error">
        <AlertTriangleIcon className="size-4" aria-hidden />
        <AlertTitle>The readiness check did not answer</AlertTitle>
        <AlertDescription>{state.message}</AlertDescription>
      </Alert>
    );
  }
  if (state.kind === "ready") {
    return (
      <Alert data-testid="readiness-ready">
        <CheckCircle2Icon className="size-4" aria-hidden />
        <AlertTitle>Ready to run</AlertTitle>
        <AlertDescription>
          {/* One child: the description is a grid, so an inline <code> would
              otherwise be laid out as its own row. */}
          <span>
            A compatible <code>main(ctx)</code> was found, every check passed, and the imports are
            available in the runner.
          </span>
        </AlertDescription>
      </Alert>
    );
  }
  if (state.kind === "unknown") {
    return (
      <Alert data-testid="readiness-unknown">
        <CircleAlertIcon className="size-4" aria-hidden />
        <AlertTitle>Partly verified — not certified ready</AlertTitle>
        <AlertDescription>
          <ul className="list-disc pl-4">
            {state.reasons.map((reason, index) => (
              <li key={index}>{reason}</li>
            ))}
          </ul>
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <Alert variant="destructive" data-testid="readiness-blocked">
      <AlertTriangleIcon className="size-4" aria-hidden />
      <AlertTitle>Fix these before creating the strategy</AlertTitle>
      <AlertDescription>
        <ul className="list-disc pl-4">
          {(state.reasons.length > 0
            ? state.reasons
            : [state.result.messages.join(" ") || "The source is not runnable."]
          ).map((issue, index) => (
            <li key={index}>{issue}</li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}

export function AddParameterField({
  onAdd,
  existing,
}: Readonly<{ onAdd: (field: SchemaField) => void; existing: string[] }>) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<SchemaField["type"]>("number");
  const [required, setRequired] = useState(false);
  const [description, setDescription] = useState("");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");

  function add() {
    const trimmed = name.trim();
    if (!trimmed) {
      toast.error("Give the parameter a name.");
      return;
    }
    if (existing.includes(trimmed)) {
      toast.error("That parameter name is already used.");
      return;
    }
    onAdd({
      name: trimmed,
      type,
      required,
      description: description.trim() || null,
      enumValues: null,
      minimum: minimum.trim() === "" ? null : Number(minimum),
      maximum: maximum.trim() === "" ? null : Number(maximum),
    });
    setName("");
    setDescription("");
    setMinimum("");
    setMaximum("");
    setRequired(false);
    setOpen(false);
  }

  if (!open) {
    return (
      <div>
        <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
          Add a parameter
        </Button>
      </div>
    );
  }
  return (
    <div className="grid gap-3 rounded-md border border-border/70 p-3 md:grid-cols-2">
      <div className="grid gap-1.5">
        <Label htmlFor="param-name">Name in ctx.params</Label>
        <Input id="param-name" value={name} onChange={(event) => setName(event.target.value)} />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="param-type">Type</Label>
        <Select value={type} onValueChange={(value) => setType(value as SchemaField["type"])}>
          <SelectTrigger id="param-type" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="string">Text</SelectItem>
            <SelectItem value="number">Number</SelectItem>
            <SelectItem value="integer">Whole number</SelectItem>
            <SelectItem value="boolean">Yes / no</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {type === "number" || type === "integer" ? (
        <div className="grid grid-cols-2 gap-3 md:col-span-2">
          <div className="grid gap-1.5">
            <Label htmlFor="param-min">Minimum (optional)</Label>
            <Input id="param-min" value={minimum} onChange={(event) => setMinimum(event.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="param-max">Maximum (optional)</Label>
            <Input id="param-max" value={maximum} onChange={(event) => setMaximum(event.target.value)} />
          </div>
        </div>
      ) : null}
      <div className="grid gap-1.5 md:col-span-2">
        <Label htmlFor="param-description">Help text (optional)</Label>
        <Input
          id="param-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>
      <label className="flex items-center gap-2 text-sm md:col-span-2">
        <Checkbox
          checked={required}
          onCheckedChange={(value) => setRequired(value === true)}
          aria-label="Required"
        />
        Required
      </label>
      <div className="flex gap-2 md:col-span-2">
        <Button type="button" size="sm" onClick={add}>
          Add
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
