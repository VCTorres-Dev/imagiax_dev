# Publication Candidate Ambiguities (Require Explicit Decision)

This file records unresolved path groups that block candidate-branch creation.

## A1: Backend half-migration boundary (`app/**` vs `src/**`)
- Current state indicates mixed migration.
- Unknown: which files are canonical, which are stale, and which are still runtime-required.
- Decision required:
  1. Canonical backend root (`src/**` only vs dual-root transitional policy),
  2. Explicit keep/drop map for `app/**` paths,
  3. Evidence that runtime/deploy behavior is preserved.

## A2: OHIF canonical vs nested mirror boundary
- Canonical OHIF source is `ohif-extension-imagiax-ai/`.
- Mirror/runtime path under `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/**` remains transitional.
- Decision required:
  1. Mirror sync policy or removal policy,
  2. Boundary handling for nested `.git`,
  3. Explicit drift/ownership rule to avoid undocumented dual-write.

## A3: OHIF non-src tooling/config scope
- `ohif-extension-imagiax-ai/**` non-`src` files may be required, but inclusion can conflict with root topology.
- Decision required:
  1. Minimal required non-`src` files,
  2. Compatibility rationale with root repo,
  3. Exclusions for generated/vendor/build outputs.

## A4: Repo-curation leftovers and snapshot boundaries
- `_backup/**`, `_quarantine/**`, and `.sdd-temp/**` contain evidence and workflow artifacts, not candidate runtime source.
- Decision required:
  1. Which documentation evidence (if any) belongs in candidate-supporting,
  2. Confirmation that backup/snapshot material stays held.

## Blocking Statement
These ambiguities are **blocking**. Candidate branch creation remains forbidden until each ambiguity has an explicit resolution record.
