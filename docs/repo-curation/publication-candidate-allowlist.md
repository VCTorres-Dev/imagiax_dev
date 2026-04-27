# Publication Candidate Allowlist (Isolation Phase)

## Scope and Safety Notes
- This document defines a **candidate proposal**, not an approval.
- Classification is conservative and based on SDD spec/design/tasks plus recent dirty-tree decisions.
- Any path not explicitly listed as candidate remains **HOLD** by default.

## candidate-core paths
These are the only source paths currently treated as publication-candidate core:

- `ohif-extension-imagiax-ai/src/**`
  - Rationale: canonical OHIF extension source-of-truth in current spec/design.

## candidate-supporting paths
These are documentation/support artifacts allowed for candidate preparation context:

- `docs/repo-curation/**`
  - Includes this allowlist/hold/ambiguity/checklist set and existing repo-curation evidence docs.

## Explicitly not approved in this file
- No approval is granted here for:
  - `src/**` (backend migration target path) until half-migration boundary is resolved.
  - Any root-level config/tooling beyond repo-curation docs.
  - Any nested OHIF mirror/runtime path under `OHIF Medical Imaging Viewer/**`.

## Hard Hold (mandatory)
**HARD HOLD**: No publication-candidate branch creation is allowed until all of the following are resolved:
1. Half-migration boundary (`app/**` vs `src/**`) is explicitly decided and documented.
2. Ambiguous boundary groups listed in `publication-candidate-ambiguities.md` are resolved.
3. Holdlist review is explicitly approved.

## No-Main-Touch Statement
**No main touch is authorized in this phase.**
This isolation phase is documentation-only. Do not perform direct `main` updates, `HEAD:main`, force-push, or integration work to `main`.
