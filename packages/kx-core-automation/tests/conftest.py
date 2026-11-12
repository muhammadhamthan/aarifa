"""Fixtures for kx.automation tests.

The root `tests/conftest.py` is not an ancestor of this directory, so pytest never loads it for
these tests. We load it by file path (no sys.path games) and re-export its DB fixtures and its
"skip db tests without KX_TEST_PG_DSN" hook, so the package runs alone or as part of the repo run.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from types import ModuleType
from typing import Any

import pytest
from automation_fakes import Clock


def _load_root_conftest() -> ModuleType:
    path = pathlib.Path(__file__).resolve().parents[3] / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("kx_root_conftest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()
pg_databases = _root.pg_databases
databases = _root.databases
pytest_collection_modifyitems = _root.pytest_collection_modifyitems

TABLES = (
    "outbound_messages",
    "inbound_messages",
    "threads",
    "message_templates",
    "channel_accounts",
    "webhook_receipts",
    "suppressions",
    "contact_suppressions",
    "opt_out_log",
    "scheduled_jobs",
    "call_attempts",
    "price_observations",
    "scrape_runs",
    "outbox_events",
    "inbox_events",
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


# ---------------------------------------------------------------- database
@pytest.fixture
async def auto_db(databases: Any) -> AsyncIterator[Any]:
    """Clean automation schema with one WhatsApp, one internal-alerts WhatsApp and one email identity
    plus approved templates."""
    from sqlalchemy import text

    from kx.core.settings import LogicalDb

    async with databases.transaction(LogicalDb.AUTOMATION) as s:
        await s.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
        await s.execute(
            text(
                "INSERT INTO channel_accounts (channel, provider, external_account_id, display_name, address, purpose, "
                "secret_ref) VALUES "
                "('whatsapp','meta_cloud','PNID1','Arifaa Pack','+971500000001','customer','arn:x'),"
                "('whatsapp','meta_cloud','PNID2','Arifaa Alerts','+971500000002','internal_alerts','arn:y'),"
                "('email','ses','sales@arifapack.com','Arifaa Pack','sales@arifapack.com','customer','arn:z')"
            )
        )
        await s.execute(
            text(
                "INSERT INTO message_templates (channel, name, category, provider_status, body, variables) VALUES "
                "('whatsapp','followup_1','utility','approved','Hello, any update on your packaging enquiry?','{}'),"
                "('whatsapp','reorder_reminder','utility','approved','Time to reorder?','{}'),"
                "('whatsapp','quote_ready','utility','approved','Your quote {{1}} total {{2}}','{}'),"
                "('whatsapp','approval_escalation','utility','approved','Quote {{1}} {{2}} waiting ({{3}})','{}'),"
                "('whatsapp','negotiation_paused','utility','approved','Buyer below range, reply by {{1}}','{}'),"
                "('whatsapp','draft_one','utility','draft','not approved','{}')"
            )
        )
    yield databases


# ---------------------------------------------------------------- real redis (Lua needs a server)
@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server not installed")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
