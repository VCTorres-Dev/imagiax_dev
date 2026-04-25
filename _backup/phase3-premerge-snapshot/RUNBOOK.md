Phase 3 Pre-merge Snapshot Runbook

This directory is remote-safe and self-contained for proof/rollback support.

Included:
- Canonical OHIF targets under ohif/canonical
- Runtime mirror snapshots under ohif/mirror-snapshot
- Repo-curation evidence docs/json and verifier scripts
- Critical backend/deploy contract files in contracts/
- manifest.json and checksums-sha256.txt

Excluded by design:
- .env*, SQL dumps, PHI/data blobs, .sdd-temp, temp folders, caches, nested .git contents

Restore/Retry guidance:
1. Use local layered backup outside repository for full restoration (committed + dirty + sensitive).
2. Use this snapshot to recover only remote-safe proof-gate artifacts.
3. Verify checksums before using files for restore.
4. Re-run runtime path and drift verification scripts before enabling any Phase 3 mutation.
