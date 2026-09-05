# Finance Image Fundamentals Package Design

## Problem

The finance image copies `/app/backend` but not the repository-root `fundamentals` Python package. `backend.api.routers.fundamentals` imports that package, so Uvicorn exits with `ModuleNotFoundError` and the container health check receives connection refused.

## Design

Copy the existing `fundamentals/` directory into `/app/fundamentals/` in the runtime image beside `/app/backend/`. Keep imports, module ownership, Compose commands, and health checks unchanged. This matches the current source layout and avoids moving modules or introducing package-install metadata during a release recovery.

## Verification

Build only the `finance-app` image, assert that Python can import `fundamentals.index_scopes` inside it, recreate the finance service, and verify that `kite-app` becomes healthy. Confirm Alembic reaches its configured head and the backend responds on port 8777 before starting dependent services.

