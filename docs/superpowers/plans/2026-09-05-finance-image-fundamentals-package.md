# Finance Image Fundamentals Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the finance image include the fundamentals package required by the backend router.

**Architecture:** Preserve the existing top-level package boundary and add it to the runtime image next to `backend/`. Validate both static image contents and the real Compose health path.

**Tech Stack:** Docker, Docker Compose, Python, FastAPI, Alembic

---

### Task 1: Include and verify the fundamentals package

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Reproduce the missing module in the current image**

Run: `docker compose run --rm --no-deps finance-app python -c "import fundamentals.index_scopes"`

Expected: FAIL with `ModuleNotFoundError: No module named 'fundamentals'`.

- [ ] **Step 2: Copy the package into the runtime image**

Add below the backend copy rule:

```dockerfile
COPY fundamentals/ /app/fundamentals/
```

- [ ] **Step 3: Build only the affected image**

Run: `docker compose build finance-app`

Expected: build succeeds.

- [ ] **Step 4: Verify the module exists in the rebuilt image**

Run: `docker compose run --rm --no-deps finance-app python -c "import fundamentals.index_scopes; print(fundamentals.index_scopes.__file__)"`

Expected: output begins with `/app/fundamentals/`.

- [ ] **Step 5: Recreate and verify the service**

Run: `docker compose up -d finance-app`

Expected: `kite-app` becomes healthy and logs contain no `ModuleNotFoundError`.

Run: `docker compose exec -T finance-app alembic -c /app/backend/alembic.ini current`

Expected: current revision is the configured migration head.

- [ ] **Step 6: Start dependent services and commit**

Run: `docker compose up -d`

Expected: Compose services start without a failed finance dependency.

Commit `Dockerfile` and these design documents with `fix(docker): include fundamentals package`.

