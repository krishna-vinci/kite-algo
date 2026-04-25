## Frontend Next Shell

Terminal-style Next.js shell for static reference pages in `frontend-next`.

### Routes

- `/custom-display` - workspace composition sketch
- `/dashboard` - operator overview draft
- `/options` - options workspace draft
- `/algos` - process manager draft
- `/alerts` - alerting draft
- `/screeners` - screener builder draft
- `/paper` - paper trading blotter mock
- `/charts` - charting surface mock
- `/settings` - trading defaults and sessions mock

### Scripts

```bash
npm run dev
npm run test
npm run lint
npm run typecheck
npm run build
```

### Notes

- All pages are static and mock-driven.
- Shell styling is locked to the dark terminal token set in `app/globals.css`.
- The root route redirects to `/dashboard`.
