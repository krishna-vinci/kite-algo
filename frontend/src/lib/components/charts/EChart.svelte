<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import * as echarts from 'echarts';

  export let options: any;
  export let style: string = "width: 100%; height: 100%;";
  export let theme: string | object | null = null;

  let chartContainer: HTMLDivElement;
  let chartInstance: echarts.ECharts | null = null;

  onMount(() => {
    if (chartContainer) {
      chartInstance = echarts.init(chartContainer, theme);
      chartInstance.setOption(options);

      const resizeObserver = new ResizeObserver(() => {
        chartInstance?.resize();
      });
      resizeObserver.observe(chartContainer);

      return () => {
        resizeObserver.disconnect();
        chartInstance?.dispose();
      };
    }
  });

  $: if (chartInstance && options) {
    chartInstance.setOption(options, true); // true = not merge, replace
  }
</script>

<div bind:this={chartContainer} {style}></div>
