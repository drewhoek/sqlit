"""IBM DB2 for i adapter using pyodbc with ODBC driver."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlit.domains.connections.providers.adapters.base import (
    ColumnInfo,
    CursorBasedAdapter,
    IndexInfo,
    SequenceInfo,
    TableInfo,
    TriggerInfo,
)

if TYPE_CHECKING:
    from sqlit.domains.connections.domain.config import ConnectionConfig


def parse_libraries(raw: str | None) -> list[str]:
    """Parse the `libraries` option into an ordered, de-duplicated list.

    Accepts commas and/or whitespace as separators. Names are upper-cased,
    matching how IBM i stores schema names, unless wrapped in double quotes
    (delimited identifiers keep their case).
    """
    if not raw:
        return []
    seen: set[str] = set()
    libraries: list[str] = []
    for token in raw.replace(",", " ").split():
        if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
            name = token[1:-1]
        else:
            name = token.upper()
        if name and name not in seen:
            seen.add(name)
            libraries.append(name)
    return libraries


class _Db2iConnection:
    """A pyodbc connection plus the library filter it was opened with.

    Other adapters stash per-connection state as `conn._sqlit_*` attributes,
    but pyodbc.Connection has no `__dict__`, so the filter rides along on this
    thin wrapper instead. Everything else is delegated to the real connection.
    """

    __slots__ = ("_inner", "libraries")

    def __init__(self, inner: Any, libraries: list[str]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "libraries", libraries)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _Db2iConnection.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


class Db2iAdapter(CursorBasedAdapter):
    """Adapter for IBM DB2 for i using pyodbc with IBM i Access ODBC Driver.

    This adapter uses the IBM i Access ODBC Driver which does not require
    an IBM license. The driver must be installed separately on the system.
    """

    @property
    def name(self) -> str:
        return "IBM DB2 for i"

    @property
    def install_extra(self) -> str:
        return "db2i"

    @property
    def install_package(self) -> str:
        return "pyodbc"

    @property
    def driver_import_names(self) -> tuple[str, ...]:
        return ("pyodbc",)

    @property
    def supports_multiple_databases(self) -> bool:
        return False

    @property
    def supports_cross_database_queries(self) -> bool:
        return True

    @property
    def supports_stored_procedures(self) -> bool:
        return True

    @property
    def supports_sequences(self) -> bool:
        return True

    @property
    def default_schema(self) -> str:
        return ""

    def build_connection_string(self, config: ConnectionConfig) -> str:
        endpoint = config.tcp_endpoint
        if endpoint is None:
            raise ValueError("DB2 for i connections require a TCP-style endpoint.")

        # Optional driver name from config, default to IBM i Access ODBC Driver
        driver_name = config.get_option("odbc_driver") or "IBM i Access ODBC Driver"

        # Format: DRIVER={driver};SYSTEM=hostname;UID=user;PWD=password;
        parts = [
            f"DRIVER={{{driver_name}}}",
            f"SYSTEM={endpoint.host}",
        ]

        # Port is optional for the IBM i Access ODBC Driver
        if endpoint.port:
            parts.append(f"PORT={endpoint.port}")

        if endpoint.username:
            parts.append(f"UID={endpoint.username}")
        if endpoint.password:
            parts.append(f"PWD={endpoint.password}")

        # DBQ = default library, then the library list. A leading empty entry
        # leaves the job's default library alone. With system naming the list
        # is what unqualified names resolve against, so the explorer filter
        # doubles as the library list.
        default_library = endpoint.database or ""
        libraries = parse_libraries(config.get_option("libraries"))
        if libraries:
            rest = [lib for lib in libraries if lib != default_library]
            parts.append("DBQ=" + ",".join([default_library, *rest]))
        elif default_library:
            parts.append(f"DBQ={default_library}")

        if config.get_option("naming", "sql") == "system":
            parts.append("NAM=1")

        # Pass any extra options straight through to the driver
        for key, value in config.extra_options.items():
            if key in ("odbc_driver", "naming", "libraries"):
                continue
            parts.append(f"{key}={value}")

        return ";".join(parts) + ";"

    def connect(self, config: ConnectionConfig) -> Any:
        pyodbc = self._import_driver_module(
            "pyodbc",
            driver_name=self.name,
            extra_name=self.install_extra,
            package_name=self.install_package,
        )
        conn = pyodbc.connect(self.build_connection_string(config))
        # Enable autocommit for DDL operations
        conn.autocommit = True
        return _Db2iConnection(conn, parse_libraries(config.get_option("libraries")))

    @staticmethod
    def _schema_filter(conn: Any, column: str, database: str | None = None) -> tuple[str, list[Any]]:
        """WHERE fragment restricting `column` to the connection's libraries.

        With no `libraries` option, hide the Q* system libraries as before.
        `database`, when given, narrows to that single library.
        """
        if database:
            return f"{column} = ? ", [database]
        libraries = getattr(conn, "libraries", None) or []
        if libraries:
            placeholders = ", ".join("?" for _ in libraries)
            return f"{column} IN ({placeholders}) ", list(libraries)
        return f"{column} NOT LIKE 'Q%' ", []

    def get_databases(self, conn: Any) -> list[str]:
        """Get list of libraries (databases) from DB2 for i."""
        # Libraries are the equivalent of databases in DB2 for i
        where, params = self._schema_filter(conn, "table_schema")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT table_schema FROM QSYS2.SYSTABLES WHERE " + where + "ORDER BY table_schema",
            params,
        )
        return [row[0] for row in cursor.fetchall()]

    def get_tables(self, conn: Any, database: str | None = None) -> list[TableInfo]:
        where, params = self._schema_filter(conn, "table_schema", database)
        cursor = conn.cursor()
        # On IBM i most data lives in DDS physical files (P), not SQL tables
        # (T); MQTs (M) and aliases (A) are table-like too. Listing only 'T'
        # hid the bulk of a legacy library.
        cursor.execute(
            "SELECT table_schema, table_name FROM QSYS2.SYSTABLES WHERE table_type IN ('T', 'P', 'M', 'A') AND " + where + "ORDER BY table_schema, table_name",
            params,
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]

    def get_views(self, conn: Any, database: str | None = None) -> list[TableInfo]:
        where, params = self._schema_filter(conn, "table_schema", database)
        cursor = conn.cursor()
        # SQL views (V) plus DDS logical files (L), which the catalog reports as
        # views but QSYS2.SYSVIEWS omits.
        cursor.execute(
            "SELECT table_schema, table_name FROM QSYS2.SYSTABLES WHERE table_type IN ('V', 'L') AND " + where + "ORDER BY table_schema, table_name",
            params,
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]

    def get_columns(self, conn: Any, table: str, database: str | None = None, schema: str | None = None) -> list[ColumnInfo]:
        cursor = conn.cursor()

        # Get primary key columns (try-catch for permissions)
        pk_columns: set[str] = set()
        if schema:
            try:
                cursor.execute(
                    "SELECT COLNAME FROM QSYS2.SYSCST "
                    "WHERE CONSTRAINT_SCHEMA = ? AND CONSTRAINT_NAME IN ("
                    "  SELECT CONSTRAINT_NAME FROM QSYS2.SYSCST "
                    "  WHERE CONSTRAINT_SCHEMA = ? AND TYPE = 'PRIMARY KEY' "
                    "  AND TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    ")",
                    (schema, schema, schema, table),
                )
                pk_columns = {row[0] for row in cursor.fetchall()}
            except Exception:
                # If primary key query fails, continue without PK info
                pass

        # Column information via the SYSIBM catalog (most compatible on IBM i)
        if schema:
            query = "SELECT COLUMN_NAME, TYPE_NAME FROM SYSIBM.SQLCOLUMNS WHERE TABLE_SCHEM = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
            params = [schema, table]
        else:
            query = "SELECT COLUMN_NAME, TYPE_NAME FROM SYSIBM.SQLCOLUMNS WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
            params = [table]

        cursor.execute(query, params)
        return [ColumnInfo(name=row[0], data_type=row[1], is_primary_key=row[0] in pk_columns) for row in cursor.fetchall()]

    def get_procedures(self, conn: Any, database: str | None = None) -> list[str]:
        where, params = self._schema_filter(conn, "routine_schema", database)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT routine_name FROM QSYS2.SYSROUTINES WHERE routine_type = 'PROCEDURE' AND " + where + "ORDER BY routine_name",
            params,
        )
        return [row[0] for row in cursor.fetchall()]

    def get_indexes(self, conn: Any, table: str, database: str | None = None, schema: str | None = None) -> list[IndexInfo]:
        cursor = conn.cursor()
        query = "SELECT index_name, is_unique FROM QSYS2.SYSINDEXES WHERE table_name = ? "
        params: list[Any] = [table]

        if schema:
            query += "AND table_schema = ? "
            params.append(schema)

        query += "ORDER BY index_name"

        cursor.execute(query, params)
        return [IndexInfo(name=row[0], is_unique=row[1] == "Y") for row in cursor.fetchall()]

    def get_triggers(self, conn: Any, table: str, database: str | None = None, schema: str | None = None) -> list[TriggerInfo]:
        cursor = conn.cursor()
        query = "SELECT trigger_name, event_manipulation FROM QSYS2.SYSTRIGGERS WHERE event_object_table = ? "
        params: list[Any] = [table]

        if schema:
            query += "AND trigger_schema = ? "
            params.append(schema)

        query += "ORDER BY trigger_name"

        cursor.execute(query, params)
        return [TriggerInfo(name=row[0], event=row[1]) for row in cursor.fetchall()]

    def get_sequences(self, conn: Any, database: str | None = None) -> list[SequenceInfo]:
        where, params = self._schema_filter(conn, "sequence_schema", database)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT sequence_name FROM QSYS2.SYSSEQUENCES WHERE " + where + "ORDER BY sequence_name",
            params,
        )
        return [SequenceInfo(name=row[0]) for row in cursor.fetchall()]

    def get_sequence_info(self, conn: Any, sequence_name: str, database: str | None = None) -> dict[str, Any]:
        """Get detailed information about a DB2 for i sequence."""
        cursor = conn.cursor()
        query = "SELECT start_value, increment, minimum_value, maximum_value, cycle_option FROM QSYS2.SYSSEQUENCES WHERE sequence_name = ? "
        params: list[Any] = [sequence_name]

        if database:
            query += "AND sequence_schema = ? "
            params.append(database)

        cursor.execute(query, params)
        row = cursor.fetchone()

        if not row:
            return {}

        return {
            "start_value": row[0],
            "increment": row[1],
            "min_value": row[2],
            "max_value": row[3],
            "cycle": row[4] == "YES",
        }

    def quote_identifier(self, name: str) -> str:
        """Quote an identifier for DB2 for i."""
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def build_select_query(self, table: str, limit: int, database: str | None = None, schema: str | None = None) -> str:
        """Build SELECT query with FETCH FIRST for DB2 for i."""
        if schema:
            quoted_table = f"{self.quote_identifier(schema)}.{self.quote_identifier(table)}"
        else:
            quoted_table = self.quote_identifier(table)
        return f"SELECT * FROM {quoted_table} FETCH FIRST {limit} ROWS ONLY"
