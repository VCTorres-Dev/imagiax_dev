# Publication Candidate Holdlist (Isolation Phase)

## Default Rule
If a path is not explicitly allowlisted as `candidate-core` or `candidate-supporting`, it is **HOLD**.

## hold-boundary paths
Paths crossing runtime, nested-repo, or unresolved migration boundaries:

- `OHIF Medical Imaging Viewer/Viewers/**`
  - Includes nested mirror/runtime extension paths.
  - Includes nested `.git` transition boundary context.
- `ohif-extension-imagiax-ai/**` (except `ohif-extension-imagiax-ai/src/**`)
  - Tooling/build/config boundary is unresolved for candidate inclusion.
- `src/**`
  - Marked HOLD pending explicit migration-boundary decision versus `app/**`.
- `app/**`
  - Marked HOLD due to half-migration ambiguity versus `src/**`.

## hold-local-only/sensitive paths
Must not be included in publication candidate:

- `_backup/**`
- `_quarantine/**`
- `.env*`
- `*.sql`
- `service_account.json`
- `OHIF Medical Imaging Viewer/Viewers/.git/**`

## hold-noise/temp/research paths
Non-publication noise and temporary research/output areas:

- `.sdd-temp/**`
- `tmp_work/**`
- Ad-hoc local temp/debug/research artifacts not explicitly allowlisted

## Hard Hold (mandatory)
**HARD HOLD**: Do not create a publication-candidate branch while backend topology remains split (`app/**` + `src/**`) or boundary ambiguities remain unresolved.

## No-Main-Touch Statement
**No main touch is authorized in this phase.**
Do not mutate `main`, do not force-push, and do not perform integration-to-main actions.
