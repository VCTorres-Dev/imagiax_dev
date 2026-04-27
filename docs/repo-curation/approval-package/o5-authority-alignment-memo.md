# O5 Authority Alignment Memo (Packaging-Only / HOLD)

## Purpose
Document O5 under the single active authority model without executing cutover and without granting approval.

## Single active authority model (current truth)
- Runtime authority for backend operation decisions is **only** `servidor_api_orthanc_v1:app`.
- Authoritative decision surfaces are active container/start paths (Dockerfile + compose + operator start surfaces already linked in gate evidence).

## Legacy host-copy assumptions (explicit non-authoritative status)
- Legacy host-copy behavior (for example `deploy.sh` file-copy flow) is **non-authoritative for runtime-authority decisions**.
- Legacy host-copy assumptions are documentation/context aids only; they do **not** define which backend runtime is authoritative.

## What this memo does and does not do
### This memo DOES
- Package O5 alignment language in-repo.
- Make the non-authoritative status explicit for legacy host-copy assumptions.

### This memo DOES NOT
- Grant approval.
- Execute cutover.
- Execute rollback.
- Retire O5 by itself.

## Required retirement/alignment artifact still missing
O5 remains open until an approval-linked retirement/alignment artifact exists that includes, at minimum:
1. Named approver identity (non-placeholder).
2. Signed approval/retirement evidence link.
3. Explicit statement that legacy host-copy assumptions are non-authoritative for runtime-authority decisions under the single active authority model.

Until those approval-linked fields are populated, O5 disposition is `AMBER/OPEN`.

## Open-state enforcement rule
If this memo ever lacks either:
- explicit non-authoritative wording for legacy host-copy assumptions, or
- explicit linkage to approval/retirement evidence,

then O5 must remain `AMBER/OPEN` and cannot be treated as closed.
