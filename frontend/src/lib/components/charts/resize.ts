/**
 * Options for the autosize Svelte action.
 */
export interface AutosizeOptions {
	onResize?: (size: { width: number; height: number }) => void;
}

/**
 * A Svelte action that uses a ResizeObserver to automatically handle element resizing.
 * @param node - The HTML element to observe.
 * @param options - Configuration options for the action.
 * @returns A Svelte action object with a `destroy` method.
 */
export function autosize(node: HTMLElement, options?: AutosizeOptions) {
	const observer = createResizeObserver(node, (rect) => {
		options?.onResize?.({ width: rect.width, height: rect.height });
	});

	return {
		destroy() {
			observer.disconnect();
		}
	};
}

/**
 * A utility function to create and manage a ResizeObserver.
 * This is a non-action helper that can be used independently.
 * @param el - The HTML element to observe.
 * @param cb - The callback function to execute on resize.
 * @returns An object with a `disconnect` method to stop observing.
 */
export function createResizeObserver(
	el: HTMLElement,
	cb: (rect: DOMRectReadOnly) => void
): { disconnect(): void } {
	// Ensure ResizeObserver is available (SSR safety).
	if (typeof ResizeObserver === 'undefined') {
		return { disconnect: () => {} };
	}

	const observer = new ResizeObserver((entries) => {
		for (const entry of entries) {
			if (entry.target === el) {
				cb(entry.contentRect);
			}
		}
	});

	observer.observe(el);

	return {
		disconnect() {
			observer.disconnect();
		}
	};
}