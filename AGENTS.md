# Platform control plane

Read `docs/product-charter.md`, `docs/p1-delivery-plan.md` and the latest `docs/p1-application-contract.md` evidence before P1 work. Contract tests do not imply deployed product features. This worktree isolates P1 from the unfinished HTML preview changes in the original directory.

Python business backend and React UI. Keep Tauri Rust limited to GUI and child-process supervision. Never embed a Python interpreter or add tenant/Kubernetes/DB business logic to Rust.

- `control_plane/main.py`: authenticated metadata API; never accept client tenant claims as authority.
- `control_plane/planner.py`: deterministic plans only. Do not mark a resource ready without observed provider readiness.
- `control_plane/worker.py`: transactional plan queue; PostgreSQL lock order is tenant -> resource -> operation.
- `control_plane/sidecar.py`: local-only HTTP bridge; no provider credentials, kubeconfig or arbitrary shell/URL proxy.
- `web/`: browser-first React UI; server authorization is mandatory even if a button is hidden.
- `desktop/`: optional Tauri GUI and standalone binary entry point.
- `migrations/`: schema changes must be explicit Alembic revisions. Update the required startup schema revision.
- `contracts/openapi.json`: generated from FastAPI with `python scripts/export_openapi.py`.

Run pytest and `npm ci && npm run build` in web. PostgreSQL concurrency checks use `python scripts/test_postgres.py` and a disposable local Docker container, never the live cluster database. Supported image may be selected with PLATFORM_TEST_POSTGRES_IMAGE.

Keep credentials, personal prompts, kubeconfig and customer source out of Git, logs and plans. Current resources are metadata/plans only; preserve this distinction in the UI/docs until apply adapters and acceptance tests exist. Do not overwrite unrelated work in adjacent `ops/` or `openhands-upstream/` directories.

Changes to persisted policy schemas (especially CapacityPlan) must include a compatibility review of every consumer image, including agent-worker and workspace-controller. Before administrators save new fields, deploy compatible readers; after deployment, verify effective_policy on the actual agent-worker and a real queued task. A Running Pod alone is not an agent readiness check. Do not remove strict quota validation merely to make an old image accept new configuration.
