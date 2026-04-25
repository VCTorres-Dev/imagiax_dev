# OHIF Drift Report (Low-Risk Measurement)

Change: `repo-curation-github-sync`  
Batch scope: low-risk OHIF reconciliation (read-only)

## Canonical vs Runtime Mirror Paths

- Canonical source path (publishable root):
  - `ohif-extension-imagiax-ai/src`
- Runtime mirror path (nested Viewers):
  - `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src`

## Command

`powershell -ExecutionPolicy Bypass -File scripts/repo-curation/measure-ohif-drift.ps1`

## Structured Artifact

- JSON report: `docs/repo-curation/ohif-drift-report.json`

## Evidence Snapshot (`generated_at=2026-04-25T01:57:05`)

- `canonical_files`: `9`
- `mirror_files`: `8`
- `shared_files`: `8`
- `hash_matches`: `6`
- `hash_mismatches`: `2`
- `canonical_only`: `1`
- `mirror_only`: `0`

### Canonical-only files

- `pipelineConfig.ts`

### Hash mismatches

1. `ImagiaxAiPanel.tsx`
   - canonical SHA256: `940730df53e95dac5fc7d645f018b9afd2761a800fec8d8d06786bf15abc899d`
   - mirror SHA256: `a438d53e1db005099c02653b3301493b8efdb7c2ee50071f39ab4e2ef8664815`
   - canonical bytes: `103829`
   - mirror bytes: `123080`
2. `services/imagiaxApi.ts`
   - canonical SHA256: `041b3b97538bbf3b1cd0c2729c0ccd2653f7c11f751ecc8b97606110c1b037ae`
   - mirror SHA256: `dc56901738f0b8477f17cbc290c1adda5e3c37a8aa1c54a6ad185c6974b8a9d0`
   - canonical bytes: `15999`
   - mirror bytes: `18232`

## Interpretation

- Drift exists between canonical and runtime mirror sources.
- No mutation/sync action was executed in this batch.
- This report is a precondition artifact for future hold-gated reconciliation work.
