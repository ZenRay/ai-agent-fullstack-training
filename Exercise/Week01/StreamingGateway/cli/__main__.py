import argparse
import asyncio
import signal
import sys

import httpx

from cli.watch import interrupt, watch_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阅读一次运行的事件流")
    parser.add_argument("base_url", help="服务地址，例如 http://127.0.0.1:8000")
    parser.add_argument("thread_id")
    parser.add_argument("message")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.base_url, args.thread_id, args.message))
    except KeyboardInterrupt:
        return 0


async def _run(base_url: str, thread_id: str, message: str) -> int:
    holder: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=base_url) as client:
        reader = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: _schedule_stop(client, holder, reader))
        try:
            result = await watch_run(
                client,
                thread_id,
                message,
                out=sys.stdout,
                on_run_id=lambda run_id: holder.__setitem__("run_id", run_id),
            )
        except asyncio.CancelledError:
            return 0
        finally:
            loop.remove_signal_handler(signal.SIGINT)
    if result.busy_run_id:
        print(f"线程正忙: {result.busy_run_id}", file=sys.stderr)
        return 1
    if result.error:
        print(result.error, file=sys.stderr)
        return 1
    if result.view.status == "incomplete":
        return 1
    return 0


def _schedule_stop(
    client: httpx.AsyncClient,
    holder: dict[str, str],
    reader: asyncio.Task[object] | None,
) -> None:
    asyncio.create_task(interrupt(client, holder.get("run_id"), reader))


if __name__ == "__main__":
    raise SystemExit(main())
