# Approval Package Canonical Index (HOLD / Non-Cutover)

## Usage
- This is the single canonical blocker table for the approval package.
- Runtime authority remains `servidor_api_orthanc_v1:app`.
- This index does not grant approval, execute cutover, or execute rollback.

## Blocker traceability table

| Blocker | Artifact ID | State | Owner | Approver | Lane | Evidence link | Last-updated |
|---|---|---|---|---|---|---|---|
| C5 | `AP-C5-INTENTIONAL-SALUD-DELTA` | `DISPOSITIONED-PENDING-APPROVAL` | `Backend release governance owner` | `TBD (required for closure)` | `internal-prep` | `./c5-salud-delta-memo.md` | `2026-04-26` |
| O5 | `AP-O5-AUTHORITY-ALIGNMENT` | `AMBER/OPEN` | `Backend release governance owner` | `TBD (required for closure)` | `internal-prep + external-pending` | `./o5-authority-alignment-memo.md` | `2026-04-26` |
| R3 | `AP-R3-LIVE-SHADOW-REHEARSAL` | `AMBER/OPEN` | `TBD-ROLLBACK-OWNER` | `TBD` | `internal-prep + external-pending` | `./r3-live-shadow-requirements.md` | `2026-04-26` |
| B2 | `AP-B2-EXECUTION-WINDOW-BUNDLE` | `RED/OPEN` | `Backend release governance owner` | `TBD` | `internal-prep + external-pending` | `./b2-window-bundle-template.md` | `2026-04-26` |
| B6 | `AP-B6-NAMED-ASSIGNMENTS` | `RED/UNASSIGNED` | `<owner-name>` | `<approver-name>` | `internal-prep + external-pending` | `./b6-assignment-record.md` | `2026-04-26` |
| E | `AP-E-EXPLICIT-APPROVAL-LINK` | `RED/UNGRANTED` | `Release authority` | `Release authority` | `internal-prep + external-pending` | `./e-explicit-approval-link.md` | `2026-04-26` |

## Compliance notes
- Each blocker (`C5`, `O5`, `R3`, `B2`, `B6`, `E`) appears exactly once in the canonical table above.
- Any future artifact pages must reference blocker IDs from this table instead of duplicating canonical status rows.
- C5 closure is prohibited while approver identity or signed approval evidence link is missing/placeholder; until then C5 remains `DISPOSITIONED-PENDING-APPROVAL`.
- O5 closure is prohibited while either (a) explicit non-authoritative wording for legacy host-copy assumptions is missing from `o5-authority-alignment-memo.md`, or (b) approval-linked retirement/alignment evidence (named approver + signed evidence link) is missing/placeholder.
- R3 closure is prohibited while a real live-shadow rehearsal artifact bundle is missing; local/mock/template-only evidence cannot close R3.
- B2 closure is prohibited while a real execution-window bundle instance is missing; template existence alone cannot close B2.
- B6 placeholders may exist only as evidence that assignment is pending; placeholders can **never** be counted as closure evidence. B6 closure is prohibited unless named rollback owner + named cutover approver + resolvable assignment evidence artifact are all present.
- E closure is prohibited unless one separate explicit approval artifact exists with named approver, timestamped decision, and references to C5/O5/R3/B2/B6 as review inputs.
