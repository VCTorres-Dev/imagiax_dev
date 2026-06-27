# IMAGIAX Handoff Package

> Start here. This directory contains the project continuation package built from the source-of-truth commit `c0482551b`. No secrets, PHI, DICOM data, or runtime code are included.

## Read in order

1. **[HANDOFF.md](./HANDOFF.md)** — architecture, source of truth, what is included/excluded, how to run locally.
2. **[DEPLOYMENT_STATUS.md](./DEPLOYMENT_STATUS.md)** — production status and the source/runtime mismatch you must resolve before deploying.
3. **[CONTINUATION_CHECKLIST.md](./CONTINUATION_CHECKLIST.md)** — P0/P1/P2 next steps for maintainers.
4. **[MIGRATION_GAP.md](./MIGRATION_GAP.md)** — missing migrations 006–010 and recovery guidance.
5. **[MANIFEST.md](./MANIFEST.md)** — artifact manifest, checksum procedure, and release instructions.

## One-line summary

The application code source of truth is `c0482551b` on `main`. Production is live at `imagiax.dev` but runs an older runtime (`api.main`) with a migration history (006–010) not present in the repo. Reconcile the deploy-source decision and the migration gap before any automated deploy; package a GitHub Release only after P0 items are resolved.