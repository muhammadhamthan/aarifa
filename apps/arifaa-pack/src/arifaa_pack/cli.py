"""`kx` entrypoint: api | worker | beat | voice | migrate | create-user (one image; ECS services api,
worker, beat (exactly one), voice, plus the one-off migrate task)."""

from __future__ import annotations

import argparse
import sys
from contextlib import AsyncExitStack
from functools import partial
from typing import Any

from kx.core.settings import LogicalDb


def _api(_: argparse.Namespace) -> None:
    import uvicorn  # noqa: PLC0415

    from arifaa_pack.config import app_settings  # noqa: PLC0415

    uvicorn.run(
        "arifaa_pack.api:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container port
        port=app_settings().api_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )


def _worker(args: argparse.Namespace) -> None:
    """Celery worker (D25). One event loop + container per process, built on its first task."""
    from arifaa_pack.celery_app import app  # noqa: PLC0415
    from arifaa_pack.config import app_settings  # noqa: PLC0415

    argv = ["worker", "--loglevel=INFO", f"--concurrency={app_settings().worker_concurrency}"]
    if args.queues:
        argv.append(f"--queues={','.join(args.queues)}")
    if args.pool:
        argv.append(f"--pool={args.pool}")
    app.worker_main(argv)


def _beat(_: argparse.Namespace) -> None:
    """Celery Beat (D25): run EXACTLY ONE (ECS desired count 1). It only sends timers; due work and long
    waits are rows in automation.scheduled_jobs, so a Beat restart loses nothing."""
    from arifaa_pack.celery_app import app  # noqa: PLC0415

    app.start(["beat", "--loglevel=INFO", "--schedule=/tmp/celerybeat-schedule"])


def _voice(_: argparse.Namespace) -> None:
    from livekit.agents import WorkerOptions, cli  # noqa: PLC0415 - optional voice extra

    from arifaa_pack.adapters import LiveKitPlugins, write_google_credentials  # noqa: PLC0415
    from arifaa_pack.config import app_settings  # noqa: PLC0415
    from arifaa_pack.container import build  # noqa: PLC0415
    from kx.agents.voice import make_livekit_entrypoint  # noqa: PLC0415

    creds = app_settings().google_credentials_json
    write_google_credentials(creds.get_secret_value() if creds else None)  # Chirp backup (D36)
    state: dict[str, Any] = {}

    async def entry(ctx: Any) -> None:
        if "fn" not in state:  # build once per worker process, inside LiveKit's event loop
            stack = AsyncExitStack()
            c = await stack.enter_async_context(build())
            minutes = partial(c.spend.add, "voice_minutes")  # D34 daily voice-minute cap
            state.update(
                stack=stack, fn=make_livekit_entrypoint(c.agent_service, LiveKitPlugins(), on_minutes=minutes)
            )
        await state["fn"](ctx)

    sys.argv = [sys.argv[0], "start"]
    cli.run_app(WorkerOptions(entrypoint_fnc=entry))


def _migrate(args: argparse.Namespace) -> None:
    """Alembic for every database (idempotent). The job queue is a table in the automation revisions now
    (automation_0003), so there is no separate queue schema to apply."""
    from arifaa_pack.migrations import upgrade  # noqa: PLC0415

    dbs = [LogicalDb(d) for d in args.db] if args.db else list(LogicalDb)
    upgrade(dbs, args.revision)


def _create_user(args: argparse.Namespace) -> None:
    """The first admin (and the break-glass account) before anyone can sign in to the CRM. Prints the
    one-time password once; the person changes it at first sign-in and enrols an authenticator app."""
    import asyncio  # noqa: PLC0415

    from arifaa_pack.container import build  # noqa: PLC0415
    from kx.crm.services.auth import create_user  # noqa: PLC0415
    from kx.crm.services.journal import Actor  # noqa: PLC0415

    async def run() -> None:
        async with build() as c, c.databases.transaction(LogicalDb.CRM) as s:
            user, password = await create_user(
                s,
                c.crm_deps,
                email=args.email,
                full_name=args.name,
                role=args.role,
                phone_e164=args.phone,
                break_glass=args.break_glass,
                actor=Actor.system("cli"),
            )
        print(f"created {user.role} {user.email} ({user.id})")
        print(f"temporary password: {password}")

    asyncio.run(run())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kx")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api").set_defaults(fn=_api)
    w = sub.add_parser("worker")
    w.add_argument("--queues", nargs="*", default=None)
    w.add_argument("--pool", default=None, help="celery pool (prefork default; solo for local debugging)")
    w.set_defaults(fn=_worker)
    sub.add_parser("beat").set_defaults(fn=_beat)
    sub.add_parser("voice").set_defaults(fn=_voice)
    m = sub.add_parser("migrate")
    m.add_argument("--db", nargs="*", choices=[d.value for d in LogicalDb])
    m.add_argument("--revision", default="head")
    m.set_defaults(fn=_migrate)
    u = sub.add_parser("create-user", help="a staff user with a one-time password (D26)")
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", required=True, choices=["md", "manager", "admin", "sales"])
    u.add_argument("--phone", default=None, help="E.164, for WhatsApp / call alerts")
    u.add_argument("--break-glass", action="store_true", help="emergency admin: every sign-in is alerted")
    u.set_defaults(fn=_create_user)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
