"""How C reaches its durable state: the connection, the transaction, the id.

Three decisions, all of them about correctness rather than style.

**Transactions belong to the service, never to a repository.** A repository
method takes a connection and uses it; it does not open one, commit, or roll
back. That is the same rule D states for ``decrease_stock`` ("Stock updates
require C's caller-owned write transaction"), and it is what makes it possible
to hold one lock across the policy evaluation *and* the reservation write --
the property the plan calls the Phase 7 boundary.

**``BEGIN IMMEDIATE``, always.** A deferred transaction upgrades its lock
lazily, so two writers can both read, both decide, and only then discover that
one of them has to fail -- which on a cap check means both saw the full budget.
``IMMEDIATE`` takes the write lock up front and serialises them.

**Ids are random, not counted.** A counter restarts at one with the process, and
against a database that survives the restart that means a second ``man_0001``
colliding with the first. Twelve hex characters of a UUID is short enough to
read in an audit log and wide enough that a collision is not a thing that
happens.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager

from app.contracts.common import ErrorCode
from app.db.core import connect, resolve_db_path

#: Seconds to wait for another writer before giving up. SQLite's own
#: ``busy_timeout`` is set from the same number by D's ``connect``.
DEFAULT_TIMEOUT_SECONDS = 5.0


def new_id(prefix: str) -> str:
    """A fresh, durable identifier. ``man_9f2c1ab34de5``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _translate(exc: sqlite3.Error, action: str) -> Exception:
    """Turn a driver failure into a named error, or report it as a bug.

    A locked or busy database is an operational condition the caller can retry;
    anything else is a defect and must not be dressed up as one.
    """
    message = str(exc)
    if "locked" in message or "busy" in message:
        from app.errors import AgentError

        return AgentError(
            ErrorCode.DB_BUSY,
            f"the commerce database was busy while trying to {action}",
            details={"action": action},
            retryable=True,
        )
    return exc


@contextmanager
def write_transaction(db_path=None):
    """One ``BEGIN IMMEDIATE`` transaction, committed on success.

    Nested use is not supported and is not needed: every write path in
    ``app/commerce`` opens exactly one, and passes the connection down.
    """
    conn = connect(resolve_db_path(db_path), timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        else:
            conn.commit()
    except sqlite3.Error as exc:  # pragma: no cover - depends on lock contention
        raise _translate(exc, "write") from exc
    finally:
        conn.close()


@contextmanager
def read_connection(db_path=None):
    """A read-only connection. Opens no transaction of its own.

    Read paths that must agree with each other still open a transaction by
    calling ``BEGIN`` themselves; this only guarantees the connection cannot
    write at all.
    """
    conn = connect(resolve_db_path(db_path), readonly=True)
    try:
        yield conn
    finally:
        conn.close()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "new_id",
    "read_connection",
    "write_transaction",
]
