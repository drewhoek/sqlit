"""IBM Db2 for i fixtures.

There is no containerised IBM i, so these fixtures only run when DB2I_HOST
points at a real system; otherwise every Db2 for i test is skipped.
"""

from __future__ import annotations

import os

import pytest

from tests.fixtures.utils import cleanup_connection, run_cli

DB2I_HOST = os.environ.get("DB2I_HOST", "")
DB2I_USER = os.environ.get("DB2I_USER", "")
DB2I_PASSWORD = os.environ.get("DB2I_PASSWORD", "")
DB2I_LIBRARY = os.environ.get("DB2I_LIBRARY", "QGPL")
DB2I_ODBC_DRIVER = os.environ.get("DB2I_ODBC_DRIVER", "IBM i Access ODBC Driver")

_SETUP_STATEMENTS = (
    "CREATE TABLE test_users (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, email VARCHAR(100))",
    "CREATE TABLE test_products (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, "
    "price DECIMAL(10,2) NOT NULL, stock INTEGER DEFAULT 0)",
    "CREATE VIEW test_user_emails AS SELECT id, name, email FROM test_users WHERE email IS NOT NULL",
    "CREATE INDEX idx_test_users_email ON test_users(email)",
    "CREATE SEQUENCE test_sequence START WITH 1 INCREMENT BY 1",
    "INSERT INTO test_users (id, name, email) VALUES "
    "(1, 'Alice', 'alice@example.com'), (2, 'Bob', 'bob@example.com'), (3, 'Charlie', 'charlie@example.com')",
    "INSERT INTO test_products (id, name, price, stock) VALUES "
    "(1, 'Widget', 9.99, 100), (2, 'Gadget', 19.99, 50), (3, 'Gizmo', 29.99, 25)",
)

_TEARDOWN_STATEMENTS = (
    "DROP VIEW test_user_emails",
    "DROP TABLE test_users",
    "DROP TABLE test_products",
    "DROP SEQUENCE test_sequence",
)


def _connection_string() -> str:
    return (
        f"DRIVER={{{DB2I_ODBC_DRIVER}}};SYSTEM={DB2I_HOST};UID={DB2I_USER};PWD={DB2I_PASSWORD};"
        f"DBQ={DB2I_LIBRARY};NAM=1;"
    )


def _run_statements(conn, statements: tuple[str, ...]) -> None:
    cursor = conn.cursor()
    for stmt in statements:
        try:
            cursor.execute(stmt)
        except Exception:
            pass
    conn.commit()
    cursor.close()


@pytest.fixture(scope="function")
def db2i_db() -> str:
    """Set up Db2 for i test objects in DB2I_LIBRARY."""
    if not DB2I_HOST:
        pytest.skip("DB2I_HOST is not set; no IBM i available")

    try:
        import pyodbc
    except ImportError:
        pytest.skip("pyodbc is not installed")

    try:
        conn = pyodbc.connect(_connection_string(), timeout=10)
    except Exception as e:
        pytest.skip(f"Failed to connect to Db2 for i: {e}")

    _run_statements(conn, _TEARDOWN_STATEMENTS)
    _run_statements(conn, _SETUP_STATEMENTS)
    conn.close()

    yield DB2I_LIBRARY

    conn = pyodbc.connect(_connection_string(), timeout=10)
    _run_statements(conn, _TEARDOWN_STATEMENTS)
    conn.close()


@pytest.fixture(scope="function")
def db2i_connection(db2i_db: str) -> str:
    """Create a sqlit CLI connection for Db2 for i and clean up after test."""
    connection_name = f"test_db2i_{os.getpid()}"

    cleanup_connection(connection_name)

    run_cli(
        "connections",
        "add",
        "db2i",
        "--name",
        connection_name,
        "--server",
        DB2I_HOST,
        "--database",
        db2i_db,
        "--username",
        DB2I_USER,
        "--password",
        DB2I_PASSWORD,
    )

    yield connection_name

    cleanup_connection(connection_name)
