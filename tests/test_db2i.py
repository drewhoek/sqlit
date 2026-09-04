"""Integration tests for IBM Db2 for i database operations."""

from __future__ import annotations

from .test_database_base import BaseDatabaseTests, DatabaseTestConfig


class TestDb2iIntegration(BaseDatabaseTests):
    """Integration tests for IBM Db2 for i database operations via CLI."""

    @property
    def config(self) -> DatabaseTestConfig:
        return DatabaseTestConfig(
            db_type="db2i",
            display_name="IBM Db2 for i",
            connection_fixture="db2i_connection",
            db_fixture="db2i_db",
            create_connection_args=lambda: [],
            uses_limit=False,
        )
