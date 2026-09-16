from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Event, Thread

import pytest
import uvicorn

from tests.acceptance.console_native_answer_fixture import fresh_native_answer_deployment
from tests.acceptance.run_console_governed import ARCHITECT, REQUESTER


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a fresh native governed-answer console")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--dist", type=Path, default=Path("apps/console/dist"))
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--architect-port", type=int, default=8130)
    parser.add_argument("--requester-port", type=int, default=8131)
    return parser.parse_args(argv)


def _start_server(*, app: object, port: int) -> tuple[uvicorn.Server, Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    if arguments.ready_file.exists():
        raise RuntimeError("native console ready file already exists")
    if not arguments.dist.is_dir():
        raise RuntimeError("compiled console bundle is unavailable")

    stopped = Event()

    def stop(_signal_number: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    monkeypatch = pytest.MonkeyPatch()
    with ExitStack() as stack:
        stack.callback(monkeypatch.undo)
        native = stack.enter_context(
            fresh_native_answer_deployment(arguments.directory, monkeypatch=monkeypatch)
        )
        requester_origin = f"http://127.0.0.1:{arguments.requester_port}"
        architect_origin = f"http://127.0.0.1:{arguments.architect_port}"
        requester = native.deployment.build_app(
            origin=requester_origin,
            dist=arguments.dist,
            actor=REQUESTER,
        )
        architect = native.deployment.build_app(
            origin=architect_origin,
            dist=arguments.dist,
            actor=ARCHITECT,
        )
        requester_server, requester_thread = _start_server(
            app=requester, port=arguments.requester_port
        )
        architect_server, architect_thread = _start_server(
            app=architect, port=arguments.architect_port
        )
        servers = (requester_server, architect_server)
        threads = (requester_thread, architect_thread)
        deadline = time.monotonic() + 30
        while not all(server.started for server in servers):
            if any(not thread.is_alive() for thread in threads):
                raise RuntimeError("native console server stopped during startup")
            if time.monotonic() >= deadline:
                raise RuntimeError("native console server startup timed out")
            time.sleep(0.05)

        ready = {
            "architect_origin": architect_origin,
            "requester_origin": requester_origin,
            "request_id": native.request_id,
        }
        temporary_ready = arguments.ready_file.with_suffix(".tmp")
        temporary_ready.write_text(json.dumps(ready, sort_keys=True), encoding="utf-8")
        temporary_ready.replace(arguments.ready_file)
        print(json.dumps(ready, sort_keys=True), flush=True)

        stopped.wait()
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=10)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("native console server did not stop")
    arguments.ready_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
