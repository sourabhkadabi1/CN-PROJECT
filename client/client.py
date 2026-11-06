import socket
import os
import time
import sys
import math
import random
import threading

from protocol.protocol import (
    MAX_FILENAME_SIZE,
    MAX_CHUNK_SIZE,
    MSG_BACKUP_START,
    MSG_RESUME_QUERY,
    MSG_CHUNK,
    MSG_BACKUP_DONE,
    REPLY_START_OK,
    REPLY_RESUME,
    REPLY_CHUNK_OK,
    REPLY_DONE_OK,
    REPLY_ERROR,
    send_line,
    receive_line,
)
from utils.checksum import calculate_sha256, calculate_sha256_bytes


# ============================================================
# SETTINGS
# ============================================================

# Why 64 KiB: the header is ~100 bytes (0.15 % overhead), a broken
# connection loses at most one chunk, and a damaged chunk is cheap to
# send again. Smaller chunks waste time on ACKs, bigger ones waste
# more bytes on every retry.
DEFAULT_CHUNK_SIZE = 64 * 1024

CONNECT_TIMEOUT = 3        # seconds to wait for the TCP connection
IO_TIMEOUT = 6             # no reply for this long -> treat connection as dead
MAX_ATTEMPTS = 8           # connection attempts per server
MAX_CHUNK_RETRIES = 10     # re-sends of ONE chunk that keeps failing its checksum
BACKOFF_START = 0.5        # wait before reconnecting; doubles up to BACKOFF_MAX
BACKOFF_MAX = 4.0

print_lock = threading.Lock()


def log(message):
    # One thread per server prints, so keep each line whole.
    with print_lock:
        print(message, flush=True)


class BackupFailed(Exception):
    """A problem that reconnecting cannot fix (server said ERROR, etc.)."""


class Stats:
    """Byte counters shared by all server threads (used for the summary)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent_bytes = 0
        self.rejected = 0
        self.reconnects = 0

    def add(self, sent=0, rejected=0, reconnects=0):
        with self.lock:
            self.sent_bytes += sent
            self.rejected += rejected
            self.reconnects += reconnects


def parse_servers(args):
    """Turn ['10.0.0.2:5000', '10.0.0.3'] into [(ip, port), ...]."""
    servers = []
    for a in args:
        ip, _, port = a.partition(":")
        servers.append((ip, int(port) if port else 5000))
    return servers


# ============================================================
# SEND ONE CHUNK  (wait for the answer; send again if damaged)
# ============================================================

def send_chunk(connection, index, data, chunk_hash, options, stats, tag):
    for attempt in range(MAX_CHUNK_RETRIES + 1):
        wire = data

        # Test helper: flip one bit "on the wire". The hash in the header is
        # still the hash of the GOOD data, so the server must notice.
        if options["corrupt"] and data and random.random() < options["corrupt"]:
            damaged = bytearray(data)
            damaged[random.randrange(len(damaged))] ^= 0x01
            wire = bytes(damaged)

        send_line(connection, MSG_CHUNK, index, len(data), chunk_hash)
        connection.sendall(wire)
        stats.add(sent=len(data))

        answer = receive_line(connection)
        if answer[0] == REPLY_CHUNK_OK and int(answer[1]) == index:
            return
        if answer[0] == REPLY_ERROR:
            raise BackupFailed("server error: " + "|".join(answer[1:]))

        # CHUNK_BAD -> the server received it damaged
        stats.add(rejected=1)
        log(f"   [{tag}] chunk {index} arrived damaged -> sending it again "
            f"({attempt + 1}/{MAX_CHUNK_RETRIES})")

    raise BackupFailed(f"chunk {index} kept failing its checksum")


# ============================================================
# ONE CONNECTION
#   BACKUP_START -> RESUME_QUERY -> send only missing chunks -> BACKUP_DONE
# Returns True when the server confirmed the whole file (DONE_OK).
# Raises ConnectionError / OSError / socket.timeout if the connection breaks.
# ============================================================

def run_session(info, ip, port, restart, options, stats, tag):
    connection = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
    connection.settimeout(IO_TIMEOUT)
    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        send_line(connection, MSG_BACKUP_START, info["filename"], info["size"],
                  info["chunk_size"], info["chunk_count"], info["file_hash"],
                  1 if restart else 0)
        answer = receive_line(connection)
        if answer[0] == REPLY_ERROR:
            raise BackupFailed("server refused the backup: " + "|".join(answer[1:]))
        if answer[0] != REPLY_START_OK:
            raise BackupFailed(f"unexpected reply to BACKUP_START: {answer[0]}")

        # "Which chunks do you already have?"  <- this is what makes resume work
        send_line(connection, MSG_RESUME_QUERY)
        answer = receive_line(connection)
        if answer[0] != REPLY_RESUME:
            raise BackupFailed(f"unexpected reply to RESUME_QUERY: {answer[0]}")
        held = {int(x) for x in answer[1].split(",") if x} if len(answer) > 1 else set()
        missing = [i for i in range(info["chunk_count"]) if i not in held]
        if held:
            log(f"   [{tag}] server already has {len(held)}/{info['chunk_count']} "
                f"chunks -> sending only the other {len(missing)}")

        with open(info["path"], "rb") as f:
            sent_this_session = 0
            for index in missing:
                f.seek(index * info["chunk_size"])
                data = f.read(info["chunk_size"])
                send_chunk(connection, index, data, info["hashes"][index],
                           options, stats, tag)
                sent_this_session += 1

                if options["delay_ms"]:                  # test helper: slow sender
                    time.sleep(options["delay_ms"] / 1000.0)

                # Test helper: kill the connection once, in the middle of the backup
                if (options["drop_after"] and sent_this_session >= options["drop_after"]
                        and not options["drop_done"].is_set()):
                    options["drop_done"].set()
                    log(f"   [{tag}] (test) breaking the connection after "
                        f"{sent_this_session} chunks")
                    connection.close()
                    raise ConnectionError("connection broken on purpose")

        send_line(connection, MSG_BACKUP_DONE)
        answer = receive_line(connection)
        if answer[0] == REPLY_DONE_OK:
            return True

        # DONE_FAILED|missing=1,2|bad=7  -> the next session asks again
        # (RESUME_QUERY) and sends exactly those chunks.
        log(f"   [{tag}] server could not verify the file: {'  '.join(answer[1:])}")
        return False
    finally:
        connection.close()


# ============================================================
# BACK UP TO ONE SERVER: reconnect and resume after any failure
# ============================================================

def send_to_server(info, ip, port, options, stats):
    """Returns (success, speed_MBps, message)."""
    tag = f"{ip}:{port}"
    start = time.perf_counter()
    backoff = BACKOFF_START
    restart = False                 # True only for the "start from zero" baseline

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if run_session(info, ip, port, restart, options, stats, tag):
                elapsed = time.perf_counter() - start
                speed = (info["size"] / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                return True, speed, "stored and verified"
            reason = "server could not verify the file"
        except ConnectionRefusedError:
            reason = "server not running"
        except socket.timeout:
            reason = "timed out"
        except (ConnectionError, OSError) as error:
            reason = f"connection problem: {error}"
        except ValueError as error:
            reason = f"bad reply: {error}"
        except BackupFailed as error:
            return False, 0, str(error)

        if attempt == MAX_ATTEMPTS:
            return False, 0, f"gave up after {attempt} attempts ({reason})"

        stats.add(reconnects=1)
        if options["no_resume"]:
            restart = True          # baseline: tell the server to forget old chunks
        log(f"   [{tag}] {reason} -> reconnecting in {backoff:.1f}s "
            f"({'starting from zero' if restart else 'will resume'}, "
            f"attempt {attempt + 1}/{MAX_ATTEMPTS})")
        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)


# ============================================================
# MAIN
# ============================================================

def main():
    # Optional flags (used for testing / measurements):
    #   --chunk-size=N   --corrupt=0.2   --drop-after=N   --delay-ms=N   --no-resume
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    options = {"corrupt": 0.0, "drop_after": 0, "delay_ms": 0.0,
               "no_resume": False, "chunk_size": DEFAULT_CHUNK_SIZE,
               "drop_done": threading.Event()}
    try:
        for flag in flags:
            name, _, value = flag.partition("=")
            if name == "--corrupt":
                options["corrupt"] = float(value)
            elif name == "--drop-after":
                options["drop_after"] = int(value)
            elif name == "--delay-ms":
                options["delay_ms"] = float(value)
            elif name == "--chunk-size":
                options["chunk_size"] = int(value)
            elif name == "--no-resume":
                options["no_resume"] = True
            else:
                raise ValueError(flag)
        if not 0 < options["chunk_size"] <= MAX_CHUNK_SIZE:
            raise ValueError("chunk size")
    except ValueError:
        print("ERROR: Unknown or invalid flag.")
        sys.exit(1)

    if len(args) < 1:
        print("Usage: python -m client.client <file_path> [ip:port ...] [flags]")
        print("Flags: --chunk-size=N --corrupt=0.2 --drop-after=N --delay-ms=N --no-resume")
        print("Examples:")
        print("  python -m client.client tests/test_small.txt")
        print("  python -m client.client tests/test_small.txt 10.0.0.2:5000 10.0.0.3:5000")
        sys.exit(1)

    file_path = args[0]

    try:
        servers = parse_servers(args[1:]) or [("127.0.0.1", 5000)]
    except ValueError:
        print("ERROR: Server must be written as ip:port (port must be a number).")
        sys.exit(1)

    if not os.path.isfile(file_path):
        print(f"ERROR: File not found: {file_path}")
        sys.exit(1)

    filename = os.path.basename(file_path)
    if len(filename.encode("utf-8")) > MAX_FILENAME_SIZE:
        print("ERROR: Filename is too long.")
        sys.exit(1)

    file_size = os.path.getsize(file_path)
    chunk_size = options["chunk_size"]
    chunk_count = max(1, math.ceil(file_size / chunk_size))

    # Split the file: one hash for the whole file, one for every chunk.
    # Computed once and reused for every server.
    try:
        file_hash = calculate_sha256(file_path)
        hashes = []
        with open(file_path, "rb") as f:
            for _ in range(chunk_count):
                hashes.append(calculate_sha256_bytes(f.read(chunk_size)))
    except OSError as error:
        print(f"ERROR: Could not read file: {error}")
        sys.exit(1)

    info = {"path": file_path, "filename": filename, "size": file_size,
            "file_hash": file_hash, "chunk_size": chunk_size,
            "chunk_count": chunk_count, "hashes": hashes}

    print("=" * 60)
    print("TCP FILE BACKUP CLIENT (chunked, resumable, replicating)")
    print("=" * 60)
    print(f"File    : {file_path} ({file_size} bytes)")
    print(f"SHA-256 : {file_hash}")
    print(f"Chunks  : {chunk_count} x {chunk_size} bytes")
    print(f"Servers : {', '.join(f'{ip}:{p}' for ip, p in servers)}\n")

    # One thread per server, so every copy is written at the same time.
    stats = Stats()
    results = [None] * len(servers)

    def worker(i, ip, port):
        log(f"-> Sending to {ip}:{port} ...")
        ok, speed, msg = send_to_server(info, ip, port, options, stats)
        results[i] = (ip, port, ok, speed, msg)
        log(f"   {'✓' if ok else '✗'} {ip}:{port} {msg} ({speed:.2f} MB/s)\n")

    threads = [threading.Thread(target=worker, args=(i, ip, p))
               for i, (ip, p) in enumerate(servers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for ip, port, ok, speed, msg in results:
        print(f"{ip}:{port:<6} {'OK  ' if ok else 'FAIL'}  {msg}")

    succeeded = sum(1 for r in results if r[2])
    needed = file_size * len(servers)
    resent = max(0, stats.sent_bytes - needed)
    print(f"\n{succeeded}/{len(results)} backups verified")
    print(f"Bytes needed: {needed} | sent: {stats.sent_bytes} | re-sent: {resent} "
          f"| rejected chunks: {stats.rejected} | reconnects: {stats.reconnects}")
    sys.exit(0 if succeeded == len(results) else 1)


if __name__ == "__main__":
    main()
