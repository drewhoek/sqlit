"""Tests for the Db2 for i `libraries` option (explorer filter + library list)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sqlit.domains.connections.providers.db2i.adapter import (
    Db2iAdapter,
    _Db2iConnection,
    parse_libraries,
)
from tests.helpers import ConnectionConfig


def _config(**options: str) -> ConnectionConfig:
    return ConnectionConfig(
        name="as400",
        db_type="db2i",
        server="scheels",
        username="user",
        password="pw",
        options=options,
    )


def _fake_pyodbc(recorded: dict[str, str]) -> SimpleNamespace:
    inner = MagicMock()
    inner.autocommit = False

    def connect(conn_str: str) -> MagicMock:
        recorded["conn_str"] = conn_str
        return inner

    return SimpleNamespace(connect=connect)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[Db2iAdapter, dict[str, str]]:
    adapter = Db2iAdapter()
    recorded: dict[str, str] = {}
    monkeypatch.setattr(adapter, "_import_driver_module", lambda *a, **k: _fake_pyodbc(recorded))
    return adapter, recorded


class TestParseLibraries:
    def test_splits_on_commas_and_whitespace_and_uppercases(self) -> None:
        assert parse_libraries("itmdataddl, ITM.DATA  itm.mtst,,") == ["ITMDATADDL", "ITM.DATA", "ITM.MTST"]

    def test_quoted_names_keep_case_and_duplicates_drop(self) -> None:
        assert parse_libraries('"MixedCase" mixedcase MIXEDCASE') == ["MixedCase", "MIXEDCASE"]

    def test_empty(self) -> None:
        assert parse_libraries(None) == []
        assert parse_libraries("  ") == []


class TestConnectionString:
    def test_libraries_become_library_list_with_default_first(self, adapter) -> None:
        db2i, _ = adapter
        cfg = _config(libraries="ITMDATADDL,ITM.DATA", naming="system")
        cfg.tcp_endpoint.database = "ITMDATADDL"
        conn_str = db2i.build_connection_string(cfg)
        assert "DBQ=ITMDATADDL,ITM.DATA;" in conn_str
        assert "NAM=1;" in conn_str
        assert "libraries=" not in conn_str

    def test_libraries_without_default_leave_default_library_alone(self, adapter) -> None:
        db2i, _ = adapter
        conn_str = db2i.build_connection_string(_config(libraries="ITMDATADDL ITMMTSTDDL"))
        assert "DBQ=,ITMDATADDL,ITMMTSTDDL;" in conn_str

    def test_no_libraries_keeps_previous_behaviour(self, adapter) -> None:
        db2i, _ = adapter
        cfg = _config()
        cfg.tcp_endpoint.database = "SCHEELS"
        conn_str = db2i.build_connection_string(cfg)
        assert "DBQ=SCHEELS;" in conn_str
        assert "NAM=" not in conn_str


class TestExplorerFilter:
    def _connect(self, adapter, **options: str):
        db2i, recorded = adapter
        conn = db2i.connect(_config(**options))
        return db2i, conn, recorded

    def test_connect_wraps_connection_and_keeps_autocommit(self, adapter) -> None:
        _, conn, _ = self._connect(adapter, libraries="ITMDATADDL")
        assert isinstance(conn, _Db2iConnection)
        assert conn.libraries == ["ITMDATADDL"]
        assert conn.autocommit is True  # set on the wrapped pyodbc connection
        conn.cursor()
        conn._inner.cursor.assert_called_once()

    def test_catalog_queries_restrict_to_libraries(self, adapter) -> None:
        db2i, conn, _ = self._connect(adapter, libraries="ITMDATADDL,ITM.DATA")
        cursor = conn._inner.cursor.return_value
        cursor.fetchall.return_value = []

        db2i.get_tables(conn)
        sql, params = cursor.execute.call_args.args
        assert "table_schema IN (?, ?)" in sql
        assert params == ["ITMDATADDL", "ITM.DATA"]

        db2i.get_views(conn)
        assert "table_schema IN (?, ?)" in cursor.execute.call_args.args[0]

        db2i.get_procedures(conn)
        assert "routine_schema IN (?, ?)" in cursor.execute.call_args.args[0]

        db2i.get_sequences(conn)
        assert "sequence_schema IN (?, ?)" in cursor.execute.call_args.args[0]

        db2i.get_databases(conn)
        assert "table_schema IN (?, ?)" in cursor.execute.call_args.args[0]

    def test_without_libraries_hides_q_system_schemas(self, adapter) -> None:
        db2i, conn, _ = self._connect(adapter)
        cursor = conn._inner.cursor.return_value
        cursor.fetchall.return_value = []

        db2i.get_tables(conn)
        sql, params = cursor.execute.call_args.args
        assert "table_schema NOT LIKE 'Q%'" in sql
        assert params == []

    def test_explicit_database_narrows_to_that_library(self, adapter) -> None:
        db2i, conn, _ = self._connect(adapter, libraries="ITMDATADDL,ITM.DATA")
        cursor = conn._inner.cursor.return_value
        cursor.fetchall.return_value = []

        db2i.get_tables(conn, database="ITM.DATA")
        sql, params = cursor.execute.call_args.args
        assert "table_schema = ?" in sql
        assert params == ["ITM.DATA"]


class TestObjectTypes:
    def test_tables_include_physical_files_and_views_include_logical_files(self, adapter) -> None:
        db2i, _ = adapter
        conn = db2i.connect(_config())
        cursor = conn._inner.cursor.return_value
        cursor.fetchall.return_value = []

        db2i.get_tables(conn)
        assert "table_type IN ('T', 'P', 'M', 'A')" in cursor.execute.call_args.args[0]

        db2i.get_views(conn)
        sql = cursor.execute.call_args.args[0]
        assert "QSYS2.SYSTABLES" in sql and "table_type IN ('V', 'L')" in sql
