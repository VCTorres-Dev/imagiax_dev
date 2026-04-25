# OHIF Runtime/Deploy Path Verification (Read-Only)

Change: `repo-curation-github-sync`  
Batch scope: low-risk OHIF preparation

## Command

`powershell -ExecutionPolicy Bypass -File scripts/repo-curation/verify-ohif-runtime-paths.ps1`

## Structured Artifact

- JSON report: `docs/repo-curation/ohif-runtime-path-verification.json`

## Evidence Snapshot (`generated_at=2026-04-25T01:57:35`)

- Overall status: `pass`
- Checks: `4`
- Missing files: `0`
- Pattern mismatches: `0`

## Verified Files and Nested Runtime References

1. `deploy_production.sh`
   - expected nested source pattern: `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src`
   - matched sample:
     - `OHIF_EXT="OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src"`
2. `start_ohif.bat`
   - expected nested runtime pattern: `OHIF Medical Imaging Viewer\Viewers`
   - matched sample:
     - `cd /d "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\Viewers"`
3. `arrancar_ohif.py`
   - expected nested runtime pattern: `OHIF Medical Imaging Viewer\Viewers\platform\app`
   - matched sample:
     - `ohif_dir = r"C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\Viewers\platform\app"`
4. `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1`
   - expected nested runtime pattern: `OHIF Medical Imaging Viewer\Viewers\platform\app`
   - matched sample:
     - `$OHIF_DIR = "c:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\Viewers\platform\app"`

## Conclusion

Current deploy/local assumptions about nested runtime paths still hold. No script content was modified.
