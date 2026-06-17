"""
Unit tests for tenant remediation schema alignment.

Covers:
- Migration SQL contains soft-dedup columns (deduped_at, deduped_into_job_id, deduped_reason)
- Migration SQL creates partial unique index with WHERE deduped_at IS NULL
- No migration creates an unsafe total unique analysis_jobs_study_user_clinic_uniq
  without a predicate
- Migration SQL adds branding_config.clinic_id and backfills from usuarios
- Migration SQL creates idx_branding_config_clinic_id
- Startup code (create_auth_tables) uses partial unique index, not total
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


class TestMigration005PartialUniqueIndex(unittest.TestCase):
    """Migration must create analysis_jobs_study_user_clinic_uniq as partial."""

    def test_migration_creates_partial_unique_index(self):
        """The unique index must have WHERE deduped_at IS NULL predicate."""
        sql = _read_migration_005()
        # Must find the CREATE UNIQUE INDEX statement (not comment lines)
        create_idx_matches = re.findall(
            r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+analysis_jobs_study_user_clinic_uniq",
            sql,
            re.IGNORECASE,
        )
        self.assertGreaterEqual(
            len(create_idx_matches), 1,
            "Must have CREATE UNIQUE INDEX IF NOT EXISTS analysis_jobs_study_user_clinic_uniq",
        )
        self.assertIn("WHERE deduped_at IS NULL", sql)

    def test_migration_partial_index_includes_clinic_id_and_where(self):
        """The partial unique index must include clinic_id and a WHERE clause."""
        sql = _read_migration_005()
        # Find the actual CREATE UNIQUE INDEX statement (not comment)
        match = re.search(
            r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+analysis_jobs_study_user_clinic_uniq\s*"
            r"ON\s+analysis_jobs\s*\([^)]*clinic_id[^)]*\)\s*"
            r"WHERE\s+deduped_at\s+IS\s+NULL",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(
            match,
            "Must find: CREATE UNIQUE INDEX ... analysis_jobs_study_user_clinic_uniq "
            "ON analysis_jobs (...clinic_id...) WHERE deduped_at IS NULL",
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
                            f"a WHERE clause. Found: {stmt.strip()[:200]}",
                        )


class TestMigration005BrandingConfigClinicId(unittest.TestCase):
    """Migration must add branding_config.clinic_id and create the index."""

    def test_migration_adds_branding_config_clinic_id(self):
        """ALTER TABLE branding_config ADD COLUMN IF NOT EXISTS clinic_id UUID."""
        sql = _read_migration_005()
        self.assertIn("branding_config", sql)
        self.assertIn("clinic_id UUID", sql)
        # Must use IF NOT EXISTS for idempotency
        # Match the specific ALTER for branding_config
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
        # Must have an UPDATE ... FROM usuarios WHERE ... bc.clinic_id IS NULL
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
        # Should have ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id UUID
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
                    f"CREATE INDEX must use IF NOT EXISTS: {stmt.strip()[:200]}",
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
                    f"ALTER TABLE ADD COLUMN must use IF NOT EXISTS: {stmt.strip()[:200]}",
                )

    def test_migration_has_no_data_deletion(self):
        """Migration must not contain DELETE or DROP statements (remediation is additive)."""
        sql = _read_migration_005()
        sql_upper = sql.upper()
        # No DELETE statements
        self.assertNotIn(
            "DELETE FROM",
            sql_upper,
            "Migration must not delete data (soft-dedup approach)",
        )
        # No DROP TABLE or DROP COLUMN statements
        self.assertNotIn(
            "DROP TABLE",
            sql_upper,
            "Migration must not drop tables",
        )


# ─── Startup code alignment tests ────────────────────────────────────────


class TestStartupCodePartialUniqueIndex(unittest.TestCase):
    """Startup code must use partial unique index with WHERE deduped_at IS NULL."""

    def _read_auth_service_source(self) -> str:
        """Read app/auth/service.py source without importing (avoids Settings validation)."""
        service_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "auth", "service.py"
        )
        with open(service_path, "r") as f:
            return f.read()

    def test_create_auth_tables_uses_partial_unique_index(self):
        """create_auth_tables must create analysis_jobs_study_user_clinic_uniq
        with WHERE deduped_at IS NULL predicate, NOT a total unique index."""
        source = self._read_auth_service_source()
        # Must contain the partial unique index creation with WHERE clause
        self.assertIn("deduped_at IS NULL", source)
        self.assertIn("analysis_jobs_study_user_clinic_uniq", source)

    def test_create_auth_tables_adds_dedup_columns(self):
        """create_auth_tables must add deduped_at, deduped_into_job_id,
        deduped_reason columns to analysis_jobs."""
        source = self._read_auth_service_source()
        self.assertIn("deduped_at TIMESTAMPTZ", source)
        self.assertIn("deduped_into_job_id", source)
        self.assertIn("deduped_reason TEXT", source)

    def test_create_auth_tables_no_unsafe_total_unique_index(self):
        """create_auth_tables must NOT create an unsafe total unique index on
        analysis_jobs (study_instance_uid, usuario_id, clinic_id) without
        a WHERE clause."""
        source = self._read_auth_service_source()
        # Find any CREATE UNIQUE INDEX on analysis_jobs for study_user_clinic
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
            # (not just a newline/whitespace then end of statement)
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