#!/usr/bin/env python3
"""Bridge TCP model endpoints across an isolated network namespace via Unix sockets."""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import os
import signal
import socket
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Sequence

LOGGER = logging.getLogger("model_socket_bridge")
BUFFER_SIZE = 256 * 1024

Connector = Callable[
    [],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]
SocketIdentity = tuple[int, int]


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(BrokenPipeError, ConnectionError, OSError):
        await writer.wait_closed()


async def _finish_writing(writer: asyncio.StreamWriter) -> None:
    """Propagate EOF without closing the readable half of the stream."""
    if writer.is_closing():
        return
    if writer.can_write_eof():
        writer.write_eof()
        with suppress(BrokenPipeError, ConnectionError, OSError):
            await writer.drain()
        return

    raw_socket = writer.get_extra_info("socket")
    if raw_socket is not None:
        with suppress(OSError):
            raw_socket.shutdown(socket.SHUT_WR)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(BUFFER_SIZE):
        writer.write(data)
        await writer.drain()
    await _finish_writing(writer)


async def _relay_bidirectionally(
    incoming_reader: asyncio.StreamReader,
    incoming_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    pumps = {
        asyncio.create_task(_pump(incoming_reader, upstream_writer)),
        asyncio.create_task(_pump(upstream_reader, incoming_writer)),
    }
    try:
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_EXCEPTION)
        failure: BaseException | None = None
        for task in done:
            if task.cancelled():
                failure = asyncio.CancelledError()
                break
            if task.exception() is not None:
                failure = task.exception()
                break

        if failure is not None:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise failure

        # FIRST_EXCEPTION returns only after both pumps finish if neither fails.
        await asyncio.gather(*pending)
    finally:
        for task in pumps:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        await asyncio.gather(
            _close_writer(incoming_writer),
            _close_writer(upstream_writer),
        )


async def _handle_connection(
    incoming_reader: asyncio.StreamReader,
    incoming_writer: asyncio.StreamWriter,
    connector: Connector,
) -> None:
    peer = incoming_writer.get_extra_info("peername")
    try:
        upstream_reader, upstream_writer = await connector()
    except asyncio.CancelledError:
        await _close_writer(incoming_writer)
        raise
    except (ConnectionError, OSError) as exc:
        LOGGER.error("bridge-connect-failed peer=%r error=%s", peer, exc)
        await _close_writer(incoming_writer)
        return

    try:
        await _relay_bidirectionally(
            incoming_reader,
            incoming_writer,
            upstream_reader,
            upstream_writer,
        )
    except asyncio.CancelledError:
        raise
    except (ConnectionError, OSError) as exc:
        LOGGER.warning("bridge-connection-ended peer=%r error=%s", peer, exc)


def _tracked_handler(
    connector: Connector,
    tasks: set[asyncio.Task[None]],
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            await _handle_connection(reader, writer, connector)
        finally:
            tasks.discard(task)

    return handle


def _socket_identity(path: Path) -> SocketIdentity:
    socket_stat = path.lstat()
    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise RuntimeError(f"Unix socket path is not a socket: {path}")
    return socket_stat.st_dev, socket_stat.st_ino


def _prepare_unix_listener(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_stat = path.lstat()
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise RuntimeError(f"Refusing to replace non-socket path: {path}")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except OSError as exc:
        if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise RuntimeError(f"Could not verify existing Unix socket {path}: {exc}") from exc
    else:
        raise RuntimeError(f"Unix socket is already accepting connections: {path}")
    finally:
        probe.close()

    path.unlink(missing_ok=True)
    LOGGER.info("removed-stale-unix-socket path=%s", path)


def _remove_owned_unix_socket(path: Path, identity: SocketIdentity) -> None:
    try:
        current_identity = _socket_identity(path)
    except FileNotFoundError:
        return
    except RuntimeError as exc:
        LOGGER.warning("bridge-socket-cleanup-skipped path=%s error=%s", path, exc)
        return

    if current_identity != identity:
        LOGGER.warning("bridge-socket-cleanup-skipped path=%s reason=identity-changed", path)
        return
    path.unlink(missing_ok=True)


@dataclass(slots=True)
class BridgeServer:
    """A running bridge and the resources owned by its listener."""

    _server: asyncio.AbstractServer
    _connection_tasks: set[asyncio.Task[None]]
    _owned_unix_socket: Path | None = None
    _owned_unix_identity: SocketIdentity | None = None
    _closed: bool = field(default=False, init=False)

    @property
    def sockets(self) -> tuple[socket.socket, ...]:
        return tuple(self._server.sockets or ())

    async def serve_forever(self) -> None:
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.close()
        await self._server.wait_closed()

        tasks = tuple(self._connection_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._owned_unix_socket is not None and self._owned_unix_identity is not None:
            _remove_owned_unix_socket(
                self._owned_unix_socket,
                self._owned_unix_identity,
            )


async def start_unix_to_tcp(
    unix_socket: str | Path,
    connect_host: str,
    connect_port: int,
    *,
    socket_mode: int = 0o600,
) -> BridgeServer:
    """Listen on a Unix socket and forward each connection to a TCP endpoint."""
    if not 0 <= socket_mode <= 0o777:
        raise ValueError(f"Invalid Unix socket mode: {socket_mode:#o}")

    unix_path = Path(unix_socket).resolve()
    _prepare_unix_listener(unix_path)
    tasks: set[asyncio.Task[None]] = set()

    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(connect_host, connect_port)

    server = await asyncio.start_unix_server(
        _tracked_handler(connect, tasks),
        path=str(unix_path),
    )
    try:
        os.chmod(unix_path, socket_mode)
        identity = _socket_identity(unix_path)
    except BaseException:
        server.close()
        await server.wait_closed()
        unix_path.unlink(missing_ok=True)
        raise

    LOGGER.info(
        "bridge-ready mode=listen-unix listen=unix:%s connect=tcp:%s:%d socket_mode=%04o",
        unix_path,
        connect_host,
        connect_port,
        socket_mode,
    )
    return BridgeServer(server, tasks, unix_path, identity)


async def start_tcp_to_unix(
    listen_host: str,
    listen_port: int,
    unix_socket: str | Path,
) -> BridgeServer:
    """Listen on TCP and forward each connection to a Unix socket."""
    unix_path = Path(unix_socket).resolve()
    tasks: set[asyncio.Task[None]] = set()

    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_unix_connection(str(unix_path))

    server = await asyncio.start_server(
        _tracked_handler(connect, tasks),
        listen_host,
        listen_port,
    )
    actual_listeners = ",".join(str(sock.getsockname()) for sock in server.sockets or ())
    LOGGER.info(
        "bridge-ready mode=listen-tcp listen=%s connect=unix:%s",
        actual_listeners,
        unix_path,
    )
    return BridgeServer(server, tasks)


def _parse_socket_mode(value: str) -> int:
    try:
        mode = int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("socket mode must be an octal integer") from exc
    if not 0 <= mode <= 0o777:
        raise argparse.ArgumentTypeError("socket mode must be between 0000 and 0777")
    return mode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    unix_parser = subparsers.add_parser(
        "listen-unix",
        help="listen on a Unix socket and connect to a TCP endpoint",
    )
    unix_parser.add_argument(
        "--listen-unix",
        "--unix-socket",
        dest="unix_socket",
        type=Path,
        required=True,
    )
    unix_parser.add_argument("--connect-host", default="127.0.0.1")
    unix_parser.add_argument("--connect-port", type=int, required=True)
    unix_parser.add_argument("--socket-mode", type=_parse_socket_mode, default=0o600)

    tcp_parser = subparsers.add_parser(
        "listen-tcp",
        help="listen on TCP and connect to a Unix socket",
    )
    tcp_parser.add_argument("--listen-host", default="127.0.0.1")
    tcp_parser.add_argument("--listen-port", type=int, required=True)
    tcp_parser.add_argument(
        "--connect-unix",
        "--unix-socket",
        dest="unix_socket",
        type=Path,
        required=True,
    )
    return parser


async def _run_until_stopped(args: argparse.Namespace) -> None:
    if args.mode == "listen-unix":
        bridge = await start_unix_to_tcp(
            args.unix_socket,
            args.connect_host,
            args.connect_port,
            socket_mode=args.socket_mode,
        )
    else:
        bridge = await start_tcp_to_unix(
            args.listen_host,
            args.listen_port,
            args.unix_socket,
        )

    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed_signals.append(signum)

    try:
        await stopped.wait()
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        await bridge.close()
        LOGGER.info("bridge-stopped")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run_until_stopped(args))
    except KeyboardInterrupt:
        return 130
    except Exception:
        LOGGER.exception("bridge-failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
