import { writable } from 'svelte/store';

export type ToastMessage = {
  id: number;
  message: string;
  type: 'success' | 'error';
};

const createToastStore = () => {
  const { subscribe, update } = writable<ToastMessage[]>([]);

  const addToast = (message: string, type: 'success' | 'error') => {
    const id = Date.now();
    update((toasts) => [...toasts, { id, message, type }]);
    setTimeout(() => {
      removeToast(id);
    }, 3000);
  };

  const removeToast = (id: number) => {
    update((toasts) => toasts.filter((t) => t.id !== id));
  };

  return {
    subscribe,
    success: (message: string) => addToast(message, 'success'),
    error: (message: string) => addToast(message, 'error')
  };
};

export const toast = createToastStore();