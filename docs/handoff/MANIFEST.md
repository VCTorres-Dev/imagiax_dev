# Release Manifest & Packaging Instructions

> Defines the published handoff release, standalone assets, exclusion rules, checksum procedure, and verification instructions for the IMAGIAX handoff.

## Release identity

```text
IMAGIAX_HANDOFF_VERSION=handoff-2026-06-27
SOURCE_COMMIT=c0482551ba1dae1d8d74ffa92138f06f617e06ba
HANDOFF_RELEASE_COMMIT=7f960427a4262d78136ea7d4e165d963d4a81b35
RELEASE_URL=https://github.com/VCTorres-Dev/imagiax_dev/releases/tag/handoff-2026-06-27
SOURCE_ARCHIVE=https://github.com/VCTorres-Dev/imagiax_dev/archive/refs/tags/handoff-2026-06-27.tar.gz
DOC_ASSETS=README.md,HANDOFF.md,DEPLOYMENT_STATUS.md,CONTINUATION_CHECKLIST.md,MIGRATION_GAP.md,MANIFEST.md,verify-report.md,CHECKSUMS_SHA256.txt
MIGRATION_STATUS=006-010 documented-gap
```

The release tag points to the handoff documentation commit (`7f960427a`) so the generated source archive contains this directory. The application source baseline remains `c0482551b`; the diff from `c0482551b` to `7f960427a` is documentation-only.

## Required release assets

| Asset | Source | Required |
|------|--------|----------|
| Source archive (.tar.gz/.zip) | GitHub-generated from tag `handoff-2026-06-27` at `7f960427a` | Yes |
| `README.md` | `docs/handoff/README.md` | Yes |
| `HANDOFF.md` | `docs/handoff/` | Yes |
| `DEPLOYMENT_STATUS.md` | `docs/handoff/` | Yes |
| `CONTINUATION_CHECKLIST.md` | `docs/handoff/` | Yes |
| `MIGRATION_GAP.md` | `docs/handoff/` | Yes |
| `MANIFEST.md` | `docs/handoff/` | Yes (this file) |
| `verify-report.md` | `openspec/changes/handoff-export/verify-report.md` | Recommended |
| `CHECKSUMS_SHA256.txt` | Generated from attached release assets | Yes |
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

Generate SHA256 for every standalone asset and attach the checksum file to the release:

```bash
# From the release artifact directory
sha256sum README.md HANDOFF.md DEPLOYMENT_STATUS.md CONTINUATION_CHECKLIST.md \
  MIGRATION_GAP.md MANIFEST.md verify-report.md > CHECKSUMS_SHA256.txt
```

Recipient verification:
```bash
# Recipient downloads assets + CHECKSUMS_SHA256.txt, then:
sha256sum -c CHECKSUMS_SHA256.txt
# Any mismatch MUST be treated as a corrupted/tampered download.
```

Record format:
```text
CHECKSUMS_SHA256=
  README.md                  <sha256>
  HANDOFF.md                 <sha256>
  DEPLOYMENT_STATUS.md       <sha256>
  CONTINUATION_CHECKLIST.md  <sha256>
  MIGRATION_GAP.md           <sha256>
  MANIFEST.md                <sha256>
  verify-report.md           <sha256>
```

## Release update instructions

> **Forbidden:** do not build the package from the dirty workspace or any local tarball. Use the GitHub-generated archive from tag `handoff-2026-06-27` and attach only reviewed standalone docs.

1. **Confirm the commits** before updating release assets:
   ```bash
   git rev-parse c0482551ba1dae1d8d74ffa92138f06f617e06ba
   git rev-parse 7f960427a4262d78136ea7d4e165d963d4a81b35
   ```
2. **Prepare standalone assets:** copy the 6 handoff docs plus `verify-report.md` to a temporary artifact directory.
3. **Generate checksums** per the procedure above.
4. **Attach or replace assets** on the existing GitHub Release `handoff-2026-06-27`.
5. **Run the verification checklist** below.

## Verification checklist (must pass before publish)

- [ ] Tag points to `7f960427a` and release notes state `c0482551b` as the application baseline.
- [ ] All standalone handoff docs and `verify-report.md` are present as release assets.
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

- Delete or replace incorrect release assets.
- If the tag itself is wrong, recreate it only after coordinating with maintainers.
- Do not alter production as part of handoff release rollback.

## Next step

Maintain the release assets and checksums whenever these docs change. Complete P0 items in [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md) before any production deployment.
