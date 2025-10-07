<script lang="ts">
	import { Select as SelectPrimitive } from "bits-ui";
	import { cn } from "$lib/utils.js";
	type $$Props = SelectPrimitive.ValueProps;
	let className: $$Props["class"] = undefined;
	export { className as class };
</script>

<SelectPrimitive.Value class={cn("flex items-center", className)} {...$$restProps}>
	<slot />
</SelectPrimitive.Value>