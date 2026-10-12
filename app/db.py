from collections.abc import Iterator

import psycopg
from fastapi import Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(url: str) -> ConnectionPool:
    # prepare_threshold=None: safe behind Supabase's transaction pooler (pgbouncer), which can't keep prepared statements.
    return ConnectionPool(url, min_size=1, max_size=10, open=False,
                          kwargs={"row_factory": dict_row, "prepare_threshold": None})


def get_conn(request: Request) -> Iterator[psycopg.Connection]:
    """One transaction per request: commits on success, rolls back if the endpoint raises."""
    with request.app.state.pool.connection() as conn:
        yield conn
