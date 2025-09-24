<script lang="ts">
  import { createEventDispatcher, tick, onMount, onDestroy } from 'svelte';
  import { toast } from '$lib/stores/toast';
  import InstrumentPicker from '$lib/components/InstrumentPicker.svelte';
  import type { InstrumentRow } from '$lib/types';
  import {
    Dialog as DialogRoot,
    DialogTrigger,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
    DialogClose
  } from '$lib/components/ui/dialog';
  import type { AlertCreateRequest, Comparator } from '$lib/types';
  import { createAlert } from '$lib/api';
  import { marketwatch } from '$lib/stores/marketwatch';

  const dispatch = createEventDispatcher();

  export let open = false;
  export let prefill: Partial<AlertCreateRequest> = {};

  // Form state
  let selectedInstrument: InstrumentRow | null = null;
  let instrument_token: number | null = null;

  let comparator: Comparator = 'gt';

  // Absolute target only
  let absolute_target: number | null = null;

  // Options
  let one_time = true;
  let name: string = '';
  let notes: string = '';

  // UX / errors
  let submitting = false;
  let inlineError: string | null = null;
  let didPrefill = false;

  $: instrument_token = selectedInstrument?.instrument_token ?? instrument_token;

  // --- Subscription management for live LTP ---
  let previousToken: number | null = null;

  $: if (instrument_token && instrument_token !== previousToken) {
    if (previousToken) {
      marketwatch.unsubscribeFromInstruments([previousToken]);
    }
    // Subscribe to the new token for 'ltp' feed
    marketwatch.subscribeToInstruments([instrument_token], 'ltp');
    previousToken = instrument_token;
  }

  onDestroy(() => {
    if (previousToken) {
      marketwatch.unsubscribeFromInstruments([previousToken]);
    }
  });
  // --- End subscription management ---

  // Live LTP from marketwatch store for selected token
  $: ltp = instrument_token ? $marketwatch.instruments[instrument_token]?.last_price : null;

  // Ensure WS connects so LTP can show when dialog is used
  onMount(() => {
    try {
      marketwatch.connect();
    } catch {
      // ignore
    }
  });

  // Ensure WS connects so LTP can show when dialog is used
  onMount(() => {
    try {
      marketwatch.connect();
    } catch {
      // ignore
    }
  });

  function validate(): string | null {
    if (!selectedInstrument?.instrument_token) {
      return 'Instrument is required.';
    }
    if (absolute_target === null || !Number.isFinite(Number(absolute_target))) {
      return 'Target price is required.';
    }
    return null;
  }

  async function onSubmit(e: Event) {
    e.preventDefault();
    inlineError = validate();
    if (inlineError) return;

    submitting = true;
    try {
      const body: AlertCreateRequest = {
        instrument_token: Number(selectedInstrument?.instrument_token),
        comparator,
        target_type: 'absolute',
        one_time
      };
      if (name.trim()) body.name = name.trim();
      if (notes.trim()) body.notes = notes.trim();

      body.absolute_target = Number(absolute_target);

      // If live LTP available, use as baseline_price so "Initial Price" shows immediately
      if (ltp != null && Number.isFinite(Number(ltp))) {
        body.baseline_price = Number(ltp);
      }

      const created = await createAlert(body);
      // Success: close and notify
      toast.success('Alert created');
      open = false;
      dispatch('created', { alert: created });

      // reset minimal state for next open
      resetForm(false);
    } catch (err: any) {
      inlineError = err?.message ?? 'Failed to create alert';
      toast.error(inlineError ?? 'Failed to create alert');
    } finally {
      submitting = false;
    }
  }

  function resetForm(clearInstrument = false) {
    comparator = 'gt';
    absolute_target = null;
    one_time = true;
    name = '';
    notes = '';
    inlineError = null;
    if (clearInstrument) {
      selectedInstrument = null;
      instrument_token = null;
    }
  }

  // Prefill handling for "Recreate" flow
  $: if (open && prefill && !didPrefill) {
    instrument_token = prefill.instrument_token ?? instrument_token;
    comparator = prefill.comparator ?? 'gt';
    absolute_target = prefill.absolute_target ?? absolute_target;
    one_time = prefill.one_time ?? true;
    name = prefill.name ?? '';
    notes = prefill.notes ?? '';
    selectedInstrument = null;
    didPrefill = true;
  }

  // When dialog closes, allow prefill to re-run next open
  $: if (!open) {
    didPrefill = false;
  }
</script>

<DialogRoot bind:open>
  <DialogTrigger class="btn btn-primary">
    Create Alert
  </DialogTrigger>

  <DialogContent class="p-0">
    <form on:submit|preventDefault={onSubmit}>
      <DialogHeader>
        <DialogTitle>Create Alert</DialogTitle>
        <DialogDescription>
          Create an absolute price alert. LTP is shown (and used as Initial Price if available).
        </DialogDescription>
      </DialogHeader>

      <!-- Body -->
      <div class="px-4 pb-4 space-y-4">
        <!-- Instrument -->
        <div class="grid gap-2">
          <label class="text-sm font-medium" for="instrument_token">Instrument</label>
          <div class="flex items-center gap-3">
            <div class="flex-1">
              <InstrumentPicker on:select={(e) => { selectedInstrument = e.detail.instrument; if (!name) name = e.detail.instrument.tradingsymbol; instrument_token = e.detail.instrument.instrument_token; }} />
            </div>
          </div>
          <input type="hidden" bind:value={instrument_token} />
          <div class="text-xs text-gray-600">LTP: {ltp != null ? ltp : '—'}</div>
          <p class="text-xs text-muted-foreground">
            Pick an instrument or paste its instrument_token.
          </p>
        </div>

        <!-- Comparator -->
        <div class="grid gap-2">
          <label class="text-sm font-medium" for="comparator">Comparator</label>
          <select id="comparator" class="input" bind:value={comparator} aria-label="Comparator">
            <option value="gt">Above ( > )</option>
            <option value="lt">Below ( &lt; )</option>
          </select>
        </div>

        <!-- Target price (absolute) -->
        <div class="grid gap-2">
          <label class="text-sm font-medium" for="absolute_target">Target price</label>
          <input id="absolute_target" class="input" type="number" step="any" bind:value={absolute_target} aria-required="true" />
        </div>

        <!-- Options -->
        <div class="grid gap-2">
          <label class="inline-flex items-center gap-2">
            <input type="checkbox" bind:checked={one_time} />
            <span>One-time alert</span>
          </label>
        </div>

        <div class="grid gap-2">
          <label class="text-sm font-medium" for="alert_name">Name (optional)</label>
          <input id="alert_name" class="input" type="text" bind:value={name} maxlength="120" />
        </div>

        <div class="grid gap-2">
          <label class="text-sm font-medium" for="alert_notes">Notes (optional)</label>
          <textarea id="alert_notes" class="textarea" rows="3" bind:value={notes}></textarea>
        </div>

        {#if inlineError}
          <div class="text-sm text-red-600">{inlineError}</div>
        {/if}
      </div>

      <!-- Footer -->
      <div class="flex items-center justify-end gap-2 border-t px-4 py-3">
        <DialogClose class="btn btn-secondary" type="button">
          Cancel
        </DialogClose>
        <button
          type="submit"
          class="btn btn-primary"
          disabled={submitting || !instrument_token || absolute_target === null}
        >
          {submitting ? 'Creating…' : 'Create'}
        </button>
      </div>
    </form>
  </DialogContent>
</DialogRoot>

<style>
  @reference "tailwindcss";
  .btn {
    @apply px-4 py-2 rounded-md font-semibold transition-colors duration-200;
  }
  .btn-primary {
    @apply bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50;
  }
  .btn-secondary {
    @apply bg-gray-200 text-gray-800 hover:bg-gray-300 disabled:opacity-50;
  }
  .input {
    @apply border border-gray-300 rounded-md px-3 py-2 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-shadow duration-200 w-full;
  }
  .textarea {
    @apply border border-gray-300 rounded-md px-3 py-2 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-shadow duration-200 w-full;
  }
</style>