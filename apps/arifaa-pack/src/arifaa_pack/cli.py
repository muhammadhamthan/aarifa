"""`kx` entrypoint: api | worker | voice | migrate (one image, three ECS services + a one-off task)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import AsyncExitStack
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
    from arifaa_pack.config import app_settings  # noqa: PLC0415
    from arifaa_pack.container import build  # noqa: PLC0415
    from kx.automation.jobs import RUNTIME_KEY  # noqa: PLC0415

    async def run() -> None:
        async with build() as c:
            options: dict[str, Any] = {
                "concurrency": app_settings().worker_concurrency,
                "additional_context": {RUNTIME_KEY: c.automation_runtime},
            }
            if args.queues:
                options["queues"] = args.queues
            await c.proc.run_worker_async(**options)

    asyncio.run(run())


def _voice(_: argparse.Namespace) -> None:
    from livekit.agents import WorkerOptions, cli  # noqa: PLC0415 - optional voice extra

    from arifaa_pack.adapters import LiveKitPlugins  # noqa: PLC0415
    from arifaa_pack.container import build  # noqa: PLC0415
    from kx.agents.voice import make_livekit_entrypoint  # noqa: PLC0415

    state: dict[str, Any] = {}

    async def entry(ctx: Any) -> None:
        if "fn" not in state:  # build once per worker process, inside LiveKit's event loop
            stack = AsyncExitStack()
            c = await stack.enter_async_context(build())
            state.update(stack=stack, fn=make_livekit_entrypoint(c.agent_service, LiveKitPlugins()))
        await state["fn"](ctx)

    sys.argv = [sys.argv[0], "start"]
    cli.run_app(WorkerOptions(entrypoint_fnc=entry))


def _migrate(args: argparse.Namespace) -> None:
    import psycopg  # noqa: PLC0415

    from arifaa_pack.container import procrastinate_conninfo  # noqa: PLC0415
    from arifaa_pack.migrations import upgrade  # noqa: PLC0415
    from kx.core.settings import get_settings  # noqa: PLC0415

    dbs = [LogicalDb(d) for d in args.db] if args.db else list(LogicalDb)
    upgrade(dbs, args.revision)
    if LogicalDb.AUTOMATION in dbs:  # job-queue schema lives in the automation DB
        from procrastinate import App, PsycopgConnector  # noqa: PLC0415

        conninfo = procrastinate_conninfo(get_settings())
        with psycopg.connect(conninfo) as conn:
            exists = conn.execute("SELECT to_regclass('public.procrastinate_jobs') IS NOT NULL").fetchone()
        if not (exists and exists[0]):

            async def apply() -> None:
                app = App(connector=PsycopgConnector(conninfo=conninfo))
                async with app.open_async():
                    await app.schema_manager.apply_schema_async()

            asyncio.run(apply())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kx")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api").set_defaults(fn=_api)
    w = sub.add_parser("worker")
    w.add_argument("--queues", nargs="*", default=None)
    w.set_defaults(fn=_worker)
    sub.add_parser("voice").set_defaults(fn=_voice)
    m = sub.add_parser("migrate")
    m.add_argument("--db", nargs="*", choices=[d.value for d in LogicalDb])
    m.add_argument("--revision", default="head")
    m.set_defaults(fn=_migrate)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
