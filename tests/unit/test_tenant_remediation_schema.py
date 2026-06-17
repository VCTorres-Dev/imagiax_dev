"""
Unit tests for tenant remediation schema alignment.

Covers:
- Migration SQL adds soft-dedup columns (deduped_at, deduped_into_job_id, deduped_reason)
- Migration SQL does NOT create analysis_jobs_study_user_clinic_uniq (defered to post-remediation)
- No migration creates a total (un-predicated) unique index on analysis_jobs
- Migration SQL adds branding_config.clinic_id and backfills from usuarios
- Migration SQL creates idx_branding_config_clinic_id
- Migration SQL adds analysis_jobs.clinic_id BEFORE soft-dedup columns (ordering)
- Migration has post-remediation comments indicating future unique index step
- Startup code (create_auth_tables) does NOT create analysis_jobs_study_user_clinic_uniq
- Startup code adds clinic_id and soft-dedup columns but defers unique index
- Startup code (create_auth_tables) adds soft-dedup columns for analysis_jobs
- Startup code creates branding_config table WITH clinic_id column
"""

import os
import re
import unittest

# ─── Migration file path ─────────────────────────────────────────────────

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "migrations"
)
_MIGRATION_005 = os.path.join(
    _MIGRATIONS_DIR, "005_tenant_remediation_schema.sql"
)


def _read_migration_005() -> str:
    """Read the remediation migration SQL file."""
    with open(_MIGRATION_005, "r") as f:
        return f.read()


def _read_all_migration_sql() -> list[str]:
    """Read all .sql migration files in order."""
    sqls = []
    for fname in sorted(os.listdir(_MIGRATIONS_DIR)):
        if fname.endswith(".sql"):
            with open(os.path.join(_MIGRATIONS_DIR, fname), "r") as f:
                sqls.append(f.read())
    return sqls


# ─── Migration content tests ──────────────────────────────────────────────


class TestMigration005SoftDedupColumns(unittest.TestCase):
    """Migration must add all three soft-dedup columns to analysis_jobs."""

    def test_migration_adds_deduped_at(self):
        """deduped_at TIMESTAMPTZ column must be in migration."""
        sql = _read_migration_005()
        self.assertIn("deduped_at TIMESTAMPTZ", sql)

    def test_migration_adds_deduped_into_job_id(self):
        """deduped_into_job_id UUID REFERENCES analysis_jobs(id) must be in migration."""
        sql = _read_migration_005()
        self.assertIn("deduped_into_job_id", sql)
        self.assertIn("analysis_jobs(id)", sql)

    def test_migration_adds_deduped_reason(self):
        """deduped_reason TEXT column must be in migration."""
        sql = _read_migration_005()
        self.assertIn("deduped_reason TEXT", sql)

    def test_all_dedup_columns_use_if_not_exists(self):
        """All ALTER TABLE ADD COLUMN statements must use IF NOT EXISTS for idempotency."""
        sql = _read_migration_005()
        # Find all ALTER TABLE ... ADD COLUMN statements in the migration
        alter_matches = re.findall(
            r"ALTER TABLE \w+ ADD COLUMN( IF NOT EXISTS)? \w+",
            sql,
        )
        for match in alter_matches:
            self.assertIn(
                "IF NOT EXISTS",
                match,
                f"ALTER TABLE ADD COLUMN must use IF NOT EXISTS: {match}",
            )


class TestMigration005NoPrematureUniqueIndex(unittest.TestCase):
    """Migration must NOT create analysis_jobs_study_user_clinic_uniq yet.

     The partial unique index WHERE deduped_at IS NULL cannot be created
     until AFTER production data remediation marks all non-canonical rows
     with deduped_at. Creating it now would fail on the 41 existing
     rows that still have deduped_at = NULL.
    """

    def test_migration_does_not_create_analysis_jobs_study_user_clinic_uniq(self):
        """Migration must NOT contain CREATE UNIQUE INDEX for
        analysis_jobs_study_user_clinic_uniq (neither total nor partial).
        This index is deferred to a post-remediation migration."""
        sql = _read_migration_005()
        # Must NOT have CREATE UNIQUE INDEX ... analysis_jobs_study_user_clinic_uniq
        # in executable SQL. SQL comments (lines starting with --) are allowed
        # to reference the future index as documentation.
        for stmt in sql.split(";"):
            # Strip comment lines to isolate executable SQL
            executable_lines = "\n".join(
                line for line in stmt.split("\n")
                if not line.strip().startswith("--")
            ).strip()
            if not executable_lines:
                continue
            stmt_upper = executable_lines.upper()
            if "CREATE" in stmt_upper and "UNIQUE" in stmt_upper and "INDEX" in stmt_upper:
                self.assertNotIn(
                    "ANALYSIS_JOBS_STUDY_USER_CLINIC_UNIQ",
                    stmt_upper,
                    "Migration must NOT create analysis_jobs_study_user_clinic_uniq "
                    "yet — it must be deferred to a post-remediation migration. "
                    f"Found: {executable_lines[:200]}"
                )

    def test_migration_has_post_remediation_note(self):
        """Migration must contain comments documenting that the unique index
        is a post-remediation step, NOT created in this migration."""
        sql = _read_migration_005()
        # Must reference "post-remediation" or "POST-REMEDIATION" somewhere
        self.assertTrue(
            "post-remediation" in sql.lower() or "POST-REMEDIATION" in sql.upper(),
            "Migration must contain a comment indicating the unique index "
            "is a post-remediation step"
        )


class TestMigration005NoUnsafeTotalUniqueIndex(unittest.TestCase):
    """No migration may create a total (un-predicated) unique index on
    analysis_jobs_study_user_clinic_uniq — that would crash production."""

    def test_no_migration_creates_unsafe_total_unique_index(self):
        """Search ALL migrations for a total unique index on
        analysis_jobs without a WHERE clause predicate.

        A total unique index named analysis_jobs_study_user_clinic_uniq
        without a WHERE clause would fail on production because of
        historical terminal duplicate rows.
        """
        all_sqls = _read_all_migration_sql()
        for i, sql in enumerate(all_sqls, start=1):
            # Find any CREATE UNIQUE INDEX ... ON analysis_jobs ... without WHERE
            # that includes study_instance_uid, usuario_id, clinic_id
            for stmt in sql.split(";"):
                stmt_upper = stmt.strip().upper()
                if (
                    "CREATE UNIQUE INDEX" in stmt_upper
                    and "ANALYSIS_JOBS" in stmt_upper
                    and "STUDY_INSTANCE_UID" in stmt_upper
                    and "USUARIO_ID" in stmt_upper
                    and "CLINIC_ID" in stmt_upper
                ):
                    # Must have WHERE clause if it's the dedup index
                    if "ANALYSIS_JOBS_STUDY_USER_CLINIC_UNIQ" in stmt_upper:
                        self.assertIn(
                            "WHERE",
                            stmt_upper,
                            f"Migration {i}: analysis_jobs_study_user_clinic_uniq MUST have "
                            f"a WHERE clause. Found: {stmt.strip()[:200]}"
                        )


class TestMigration005ColumnOrdering(unittest.TestCase):
    """analysis_jobs.clinic_id must appear BEFORE soft-dedup columns in the
    migration to ensure it exists when referenced by future indexes."""

    def test_clinic_id_appears_before_dedup_columns_in_migration(self):
        """analysis_jobs.clinic_id ALTER must come BEFORE deduped_at
        in the migration SQL, because future indexes reference clinic_id."""
        sql = _read_migration_005()
        # Find position of "ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id"
        # and "ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS deduped_at"
        clinic_match = re.search(
            r"ALTER\s+TABLE\s+analysis_jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+clinic_id",
            sql, re.IGNORECASE,
        )
        dedup_match = re.search(
            r"ALTER\s+TABLE\s+analysis_jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+deduped_at",
            sql, re.IGNORECASE,
        )
        self.assertIsNotNone(
            clinic_match,
            "Migration must include ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id"
        )
        self.assertIsNotNone(
            dedup_match,
            "Migration must include ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS deduped_at"
        )
        self.assertLess(
            clinic_match.start(),
            dedup_match.start(),
            "analysis_jobs.clinic_id must appear BEFORE deduped_at in migration"
        )

    def test_clinic_id_appears_before_any_where_clause_referencing_it(self):
        """clinic_id column must be created before any index or constraint
        that references it in the migration."""
        sql = _read_migration_005()
        clinic_alter = re.search(
            r"ALTER\s+TABLE\s+analysis_jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+clinic_id",
            sql, re.IGNORECASE,
        )
        self.assertIsNotNone(clinic_alter, "Migration must create analysis_jobs.clinic_id")
        # Since we removed the unique index, this should pass trivially,
        # but we guard against future regressions.
        # Any CREATE INDEX or CREATE UNIQUE INDEX referencing analysis_jobs and clinic_id
        # must come after clinic_alter
        idx_matches = list(re.finditer(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+\w+\s+"
            r"ON\s+analysis_jobs\s*\([^)]*clinic_id[^)]*\)",
            sql, re.IGNORECASE,
        ))
        for idx_match in idx_matches:
            self.assertGreater(
                idx_match.start(),
                clinic_alter.start(),
                "clinic_id index must come after clinic_id column ALTER"
            )


class TestMigration005BrandingConfigClinicId(unittest.TestCase):
    """Migration must add branding_config.clinic_id and create the index."""

    def test_migration_adds_branding_config_clinic_id(self):
        """ALTER TABLE branding_config ADD COLUMN IF NOT EXISTS clinic_id UUID."""
        sql = _read_migration_005()
        self.assertIn("branding_config", sql)
        self.assertIn("clinic_id UUID", sql)
        # Must use IF NOT EXISTS for idempotency
        alter_match = re.search(
            r"ALTER TABLE branding_config ADD COLUMN IF NOT EXISTS clinic_id UUID",
            sql,
            re.IGNORECASE,
        )
        self.assertIsNotNone(
            alter_match,
            "Must have idempotent ALTER for branding_config.clinic_id",
        )

    def test_migration_backfills_branding_config_clinic_id_from_usuarios(self):
        """Migration must backfill branding_config.clinic_id from owner usuarios."""
        sql = _read_migration_005()
        self.assertIn("UPDATE branding_config", sql)
        self.assertIn("usuarios", sql)
        self.assertIn("bc.clinic_id IS NULL", sql)

    def test_migration_creates_idx_branding_config_clinic_id(self):
        """Migration must create idx_branding_config_clinic_id index."""
        sql = _read_migration_005()
        self.assertIn("idx_branding_config_clinic_id", sql)

    def test_migration_adds_analysis_jobs_clinic_id(self):
        """Migration must also ensure analysis_jobs.clinic_id exists."""
        sql = _read_migration_005()
        alter_match = re.search(
            r"ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id UUID",
            sql,
            re.IGNORECASE,
        )
        self.assertIsNotNone(
            alter_match,
            "Must have idempotent ALTER for analysis_jobs.clinic_id",
        )


class TestMigration005IdempotencyAndSafety(unittest.TestCase):
    """All DDL must be idempotent (IF NOT EXISTS / IF NOT NULL)."""

    def test_all_create_index_use_if_not_exists(self):
        """All CREATE INDEX statements must use IF NOT EXISTS."""
        sql = _read_migration_005()
        for stmt in sql.split(";"):
            stmt_upper = stmt.strip().upper()
            if stmt_upper.startswith("CREATE") and "INDEX" in stmt_upper:
                self.assertIn(
                    "IF NOT EXISTS",
                    stmt_upper,
                    f"CREATE INDEX must use IF NOT EXISTS: {stmt.strip()[:200]}"
                )

    def test_all_alter_table_add_column_use_if_not_exists(self):
        """All ALTER TABLE ADD COLUMN must use IF NOT EXISTS."""
        sql = _read_migration_005()
        for stmt in sql.split(";"):
            stmt_upper = stmt.strip().upper()
            if "ALTER TABLE" in stmt_upper and "ADD COLUMN" in stmt_upper:
                self.assertIn(
                    "IF NOT EXISTS",
                    stmt_upper,
                    f"ALTER TABLE ADD COLUMN must use IF NOT EXISTS: {stmt.strip()[:200]}"
                )

    def test_migration_has_no_data_deletion(self):
        """Migration must not contain DELETE or DROP statements (remediation is additive)."""
        sql = _read_migration_005()
        sql_upper = sql.upper()
        self.assertNotIn(
            "DELETE FROM",
            sql_upper,
            "Migration must not delete data (soft-dedup approach)",
        )
        self.assertNotIn(
            "DROP TABLE",
            sql_upper,
            "Migration must not drop tables",
        )


# ─── Startup code alignment tests ────────────────────────────────────────


class TestStartupCodeDeferredUniqueIndex(unittest.TestCase):
    """Startup code must NOT create analysis_jobs_study_user_clinic_uniq
    before production data remediation marks duplicates."""

    def _read_auth_service_source(self) -> str:
        """Read app/auth/service.py source without importing (avoids Settings validation)."""
        service_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "auth", "service.py"
        )
        with open(service_path, "r") as f:
            return f.read()

    def test_create_auth_tables_does_not_create_unique_index(self):
        """create_auth_tables must NOT create analysis_jobs_study_user_clinic_uniq
        — even as a partial index. This is deferred to post-remediation."""
        source = self._read_auth_service_source()
        # Must NOT contain a CREATE UNIQUE INDEX for analysis_jobs_study_user_clinic_uniq
        # as executable SQL (comments mentioning it as "do not create" are fine)
        # Search for the pattern in non-comment lines
        pattern = re.compile(
            r'CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+'
            r'analysis_jobs_study_user_clinic_uniq',
            re.IGNORECASE,
        )
        # Only check non-comment lines (lines not starting with #)
        for line in source.split('\n'):
            stripped = line.strip()
            # Skip Python comment lines
            if stripped.startswith('#') or stripped.startswith('"') or stripped.startswith("'"):
                continue
            # Check string literals that contain SQL
            if pattern.search(stripped):
                self.fail(
                    f"create_auth_tables must NOT create analysis_jobs_study_user_clinic_uniq "
                    f"yet. Found in executable line: {stripped[:200]}"
                )

    def test_create_auth_tables_adds_dedup_columns(self):
        """create_auth_tables must add deduped_at, deduped_into_job_id,
        deduped_reason columns to analysis_jobs."""
        source = self._read_auth_service_source()
        self.assertIn("deduped_at TIMESTAMPTZ", source)
        self.assertIn("deduped_into_job_id", source)
        self.assertIn("deduped_reason TEXT", source)

    def test_create_auth_tables_adds_clinic_id_before_dedup(self):
        """create_auth_tables must add analysis_jobs.clinic_id before
        soft-dedup columns, ensuring correct ordering."""
        source = self._read_auth_service_source()
        clinic_pos = source.find("ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id UUID")
        dedup_pos = source.find("ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS deduped_at TIMESTAMPTZ")
        self.assertGreater(clinic_pos, -1, "Must add analysis_jobs.clinic_id column")
        self.assertGreater(dedup_pos, -1, "Must add analysis_jobs.deduped_at column")
        self.assertLess(
            clinic_pos, dedup_pos,
            "clinic_id column must be added BEFORE deduped_at in create_auth_tables"
        )

    def test_create_auth_tables_has_post_remediation_comment(self):
        """Startup code must have a comment explaining the unique index
        is deferred to post-remediation, not auto-created."""
        source = self._read_auth_service_source()
        # Must contain a comment referencing post-remediation or DO NOT CREATE
        lines = source.split('\n')
        has_post_remediation_comment = any(
            'analysis_jobs_study_user_clinic_uniq' in line
            and ('post-remediation' in line.lower() or 'DO NOT CREATE' in line)
            for line in lines
        )
        self.assertTrue(
            has_post_remediation_comment,
            "Startup code must contain a comment explaining that "
            "analysis_jobs_study_user_clinic_uniq is deferred to post-remediation"
        )

    def test_create_auth_tables_no_unsafe_total_unique_index(self):
        """create_auth_tables must NOT create an unsafe total unique index on
        analysis_jobs (study_instance_uid, usuario_id, clinic_id) without
        a WHERE clause."""
        source = self._read_auth_service_source()
        # Search for any CREATE UNIQUE INDEX on analysis_jobs for study_user_clinic
        # that does NOT have WHERE deduped_at IS NULL
        pattern = re.compile(
            r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+"
            r"analysis_jobs_study_user_clinic_uniq\s*"
            r"ON\s+analysis_jobs\s*\([^)]*study_instance_uid[^)]*"
            r"usuario_id[^)]*clinic_id[^)]*\)",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(source):
            # Get the text after the closing paren of the ON clause
            after_match = source[match.end():]
            # The next significant token must be WHERE deduped_at IS NULL
            after_stripped = after_match.strip()
            if not after_stripped.upper().startswith("WHERE"):
                # This is an unsafe total unique index
                self.fail(
                    f"Unsafe total unique index found without WHERE clause: "
                    f"{match.group()[:200]}"
                )
            self.assertIn(
                "DEDUPED_AT IS NULL",
                after_stripped[:60].upper(),
                "Unique index must have WHERE deduped_at IS NULL predicate",
            )

    def test_create_branding_tables_includes_clinic_id(self):
        """create_branding_tables must include clinic_id in the CREATE TABLE
        statement so fresh databases get it from the start."""
        db_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "informes", "db.py"
        )
        with open(db_path, "r") as f:
            source = f.read()
        self.assertIn("clinic_id", source)

    def test_create_branding_tables_creates_clinic_id_index(self):
        """create_branding_tables must create idx_branding_config_clinic_id."""
        db_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "informes", "db.py"
        )
        with open(db_path, "r") as f:
            source = f.read()
        self.assertIn("idx_branding_config_clinic_id", source)


if __name__ == "__main__":
    unittest.main()