# Release Manifest & Packaging Instructions

> Defines the artifact manifest, exclusion rules, checksum procedure, and release instructions for the IMAGIAX handoff. No release is published by this task — this prepares the package and instructions so a maintainer can publish later.

## Release identity

```text
IMAGIAX_HANDOFF_VERSION=handoff-YYYY-MM-DD
SOURCE_COMMIT=c0482551ba1dae1d8d74ffa92138f06f617e06ba
SOURCE_ARCHIVE=<github-generated-url-to-source-tarball>
DOC_ASSETS=HANDOFF.md,DEPLOYMENT_STATUS.md,CONTINUATION_CHECKLIST.md,MIGRATION_GAP.md,MANIFEST.md
MIGRATION_STATUS=006-010 missing_or_recovered:<recovered|documented-gap|pending>
```

> Replace `YYYY-MM-DD` with the actual release date and fill `SOURCE_ARCHIVE` after the GitHub Release is created. Update `MIGRATION_STATUS` to reflect the resolved state from [`MIGRATION_GAP.md`](./MIGRATION_GAP.md) before publishing.

## Required release assets

| Asset | Source | Required |
|------|--------|----------|
| Source archive (.tar.gz/.zip) | GitHub-generated from tag at `c0482551b` | Yes |
| `HANDOFF.md` | `docs/handoff/` | Yes |
| `DEPLOYMENT_STATUS.md` | `docs/handoff/` | Yes |
| `CONTINUATION_CHECKLIST.md` | `docs/handoff/` | Yes |
| `MIGRATION_GAP.md` | `docs/handoff/` | Yes |
| `MANIFEST.md` | `docs/handoff/` | Yes (this file) |
| Curated onboarding docs | `docs/onboarding/` (only if redacted/reviewed) | Optional |

## Exclusion manifest

The source archive and any attached assets **MUST NOT** include any of the following. These are excluded from the repository by `.gitignore` and must also be excluded from any manually built package.

| Category | Patterns |
|----------|----------|
| Env / secrets | `.env*` (except `.env.example`), `service_account.json` |
| Private keys | `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `*.pub` |
| PHI / DICOM | `*.dcm`, `*.dicom`, `datos_nih/`, `datos_hd/`, `datos_medicos/`, `rx tx_/`, `RX Dicom Veridicos/` |
| Patient reports | `Reporte_IA_Pacientes_*.xlsx`, `Reporte_Tecnico_*.pdf`, `*.pdf` (report outputs) |
| Model weights | `*.pth`, `models/` |
| DB / backups | database dumps, backup snapshots |
| Caches / temp | `__pycache__/`, `.pytest_cache/`, `node_modules/`, `dist/`, `build/` |
| Untracked WIP | `src/` (uncommitted runtime), local `_backup/` |

## Checksum procedure

Generate SHA256 for every asset and record it in this manifest before publishing:

```bash
# From the release artifact directory
sha256sum *.md > CHECKSUMS_SHA256.txt
sha256sum <source-archive>.tar.gz >> CHECKSUMS_SHA256.txt
sha256sum <source-archive>.zip >> CHECKSUMS_SHA256.txt
```

Recipient verification:
```bash
# Recipient downloads assets + CHECKSUMS_SHA256.txt, then:
sha256sum -c CHECKSUMS_SHA256.txt
# Any mismatch MUST be treated as a corrupted/tampered download.
```

Record format (fill before publish):
```text
CHECKSUMS_SHA256=
  HANDOFF.md                 <sha256>
  DEPLOYMENT_STATUS.md       <sha256>
  CONTINUATION_CHECKLIST.md  <sha256>
  MIGRATION_GAP.md           <sha256>
  MANIFEST.md                <sha256>
  <source-archive>.tar.gz     <sha256>
  <source-archive>.zip        <sha256>
```

## Release instructions (GitHub Release, from `c0482551b` only)

> **Forbidden:** do not build the package from the dirty workspace or any local tarball. The release tag MUST point to `c0482551b`.

1. **Confirm the commit** before tagging:
   ```bash
   git rev-parse HEAD               # MUST equal c0482551ba1dae1d8d74ffa92138f06f617e06ba
   git log --oneline -1            # "Merge pull request #16 ... fix/tenant-remediation-schema"
   ```
2. **Create a draft release / tag** at `c0482551b` (do not publish yet).
3. **Attach assets:** copy the 5 handoff docs and let GitHub generate the source archive (`.tar.gz` + `.zip`).
4. **Generate checksums** per the procedure above and attach `CHECKSUMS_SHA256.txt`. Fill the `CHECKSUMS_SHA256` block above.
5. **Write release notes** summarizing: handoff purpose, source commit, known risks (missing migrations 006–010, source/runtime mismatch, bootstrap mode active).
6. **Run the verification checklist** (below). Fail → fix → re-run. Do not publish until all pass.
7. **Have a second reviewer verify** the manifest, exclusions, and commit, then **publish**.

## Verification checklist (must pass before publish)

- [ ] Tag points to `c0482551b` (release notes state this commit as source of truth).
- [ ] All 5 handoff docs present as release assets.
- [ ] `sha256sum -c CHECKSUMS_SHA256.txt` passes for every asset.
- [ ] No secret/PHI patterns found in any asset (see scan below).
- [ ] `MIGRATION_GAP.md` references 006–010.
- [ ] `DEPLOYMENT_STATUS.md` states production is not running `c0482551b`.
- [ ] Exclusion scan passes (no `.env*`, `service_account.json`, DICOM, DB dumps, `src/`, caches).

### Exclusion / secret scan

```bash
# Run against the release asset directory. MUST return zero matches.
grep -rIlE '\.env\.production|service_account\.json|BEGIN (RSA|EC|PRIVATE) PRIVATE KEY|POSTGRES_PASSWORD=.*[^A]' .
grep -rIlE '\.dcm$|\.dicom$|datos_medicos|datos_hd|RX Dicom' .
grep -rIl '__pycache__|node_modules|\.pytest_cache' .
```

> A PHI scan for patient names/DICOM UIDs/medical record numbers should also return zero. If a draft release includes any such file, the release MUST NOT be published until the offending file is removed.

## Rollback

Rolling back this packaging change is repository/release-metadata only — no runtime deploy occurs:

- Delete the draft release and attached assets.
- Delete the tag if it was created.
- Close the docs PR/branch; keep `main` at `c0482551b`.

## Next step

Complete P0 items in [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md) before publishing. Release publication is out of scope for this handoff commit.