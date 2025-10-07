<script lang="ts">
  import CreateAlertDialog from '$lib/components/alerts/CreateAlertDialog.svelte';
  import AlertsTable from '$lib/components/alerts/AlertsTable.svelte';
  import type { AlertCreateRequest } from '$lib/types';

  let tableRef: { refresh?: () => Promise<void> } | null = null;

  let createOpen = false;
  let createPrefill: Partial<AlertCreateRequest> = {};

  function handleCreated() {
    tableRef?.refresh?.();
  }

  function handleRecreate(e: CustomEvent<{ alert: any }>) {
    const a = e.detail.alert;
    createPrefill = {
      instrument_token: a.instrument_token,
      comparator: a.comparator,
      absolute_target: a.absolute_target,
      one_time: a.one_time,
      name: a.name ?? undefined,
      notes: a.notes ?? undefined,
      target_type: 'absolute'
    };
    createOpen = true;
  }
</script>

<section class="max-w-7xl mx-auto p-4 space-y-6">
  <header class="flex items-center justify-between">
    <h1 class="text-2xl font-semibold">Alerts</h1>
    <CreateAlertDialog bind:open={createOpen} prefill={createPrefill} on:created={handleCreated} />
  </header>

  <AlertsTable bind:this={tableRef} on:recreate={handleRecreate} />
</section>

