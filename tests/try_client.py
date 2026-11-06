# ============================================================
# TINY TEST CLIENT - only for testing the server on its own.
# (Member 2's real client replaces this.)
#
#   python -m tests.try_client FILE HOST PORT [options]
#     --chunk-size N      bytes per chunk (default 65536)
#     --corrupt N         damage chunk N the first time it is sent
#     --drop-after N      break the connection after N chunks, then resume
#     --no-resume         after a break, start again from zero
#     --delay MS          wait MS milliseconds after every chunk (slow transfer, for tests)
# ============================================================

import os
import socket
import sys
import time

from protocol.protocol import (
    send_line, receive_line,
    MSG_BACKUP_START, MSG_RESUME_QUERY, MSG_CHUNK, MSG_BACKUP_DONE,
)
from utils.checksum import calculate_sha256, calculate_sha256_bytes

args = sys.argv[1:]


def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default


file_path, host, port = args[0], args[1], int(args[2])
chunk_size = int(option("--chunk-size", 65536))
corrupt_index = int(option("--corrupt", -1))
drop_after = int(option("--drop-after", 0))
resume = "--no-resume" not in args
delay = float(option("--delay", 0)) / 1000.0

file_size = os.path.getsize(file_path)
chunk_count = max(1, -(-file_size // chunk_size))
file_hash = calculate_sha256(file_path)
chunks_sent_total = 0
corrupted_once = False
dropped_once = False


def read_chunk(index):
    with open(file_path, "rb") as file:
        file.seek(index * chunk_size)
        return file.read(chunk_size)


def attempt(number):
    global chunks_sent_total, corrupted_once, dropped_once
    connection = socket.create_connection((host, port), timeout=10)
    try:
        restart = 1 if (number > 0 and not resume) else 0
        send_line(connection, MSG_BACKUP_START, os.path.basename(file_path), file_size,
                  chunk_size, chunk_count, file_hash, restart)
        print("  START ->", receive_line(connection))

        send_line(connection, MSG_RESUME_QUERY)
        reply = receive_line(connection)
        held = {int(x) for x in reply[1].split(",") if x}
        print(f"  server already holds {len(held)} chunks")

        sent_this_time = 0
        for index in range(chunk_count):
            if index in held:
                continue
            if drop_after and not dropped_once and sent_this_time >= drop_after:
                dropped_once = True
                print("  !! breaking the connection on purpose")
                connection.close()
                return False

            data = read_chunk(index)
            for _try in range(5):
                wire = data
                if index == corrupt_index and not corrupted_once:
                    corrupted_once = True
                    wire = bytes([data[0] ^ 1]) + data[1:]        # flip one bit
                send_line(connection, MSG_CHUNK, index, len(data), calculate_sha256_bytes(data))
                connection.sendall(wire)
                chunks_sent_total += 1
                answer = receive_line(connection)
                if answer[0] == "CHUNK_OK":
                    break
                print(f"  chunk {index}: server said {answer[0]} - resending")
            sent_this_time += 1
            time.sleep(delay)

        send_line(connection, MSG_BACKUP_DONE)
        answer = receive_line(connection)
        print("  DONE ->", answer)
        return answer[0] == "DONE_OK"
    finally:
        connection.close()


start = time.time()
for number in range(10):
    print(f"attempt {number + 1}")
    try:
        if attempt(number):
            print(f"BACKUP OK in {time.time() - start:.1f}s, "
                  f"{chunks_sent_total} chunks sent (file has {chunk_count})")
            sys.exit(0)
    except (ConnectionError, OSError) as error:
        print("  connection problem:", error)
    time.sleep(1)
print("BACKUP FAILED")
sys.exit(1)
