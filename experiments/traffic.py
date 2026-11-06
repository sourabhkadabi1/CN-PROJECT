# ============================================================
# TCP BACKUP SYSTEM
# TRAFFIC GENERATOR  (runs INSIDE the Mininet hosts)
#
# A simple sender and receiver, so the network can be measured
# without depending on the backup client/server code.
#
#   python3 -m experiments.traffic receiver 10.0.0.2 6000 out.csv
#   python3 -m experiments.traffic sender   10.0.0.2 6000 25        (25 seconds)
#
# The receiver writes "timestamp,bytes" every 0.1 s: how many bytes arrived
# in that time. measure.py turns this into throughput over time.
# ============================================================

import socket
import sys
import time

BLOCK = b"x" * 65536


def receiver(host, port, out_path):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    connection, _ = server.accept()
    connection.settimeout(0.05)

    with open(out_path, "w") as file:
        file.write("timestamp,bytes\n")
        window_start = time.time()
        count = 0
        while True:
            try:
                data = connection.recv(65536)
                if not data:
                    break                      # sender finished
                count += len(data)
            except socket.timeout:
                pass

            now = time.time()
            if now - window_start >= 0.1:
                file.write(f"{now:.3f},{count}\n")
                file.flush()
                count = 0
                window_start = now
        file.write(f"{time.time():.3f},{count}\n")

    connection.close()
    server.close()


def sender(host, port, seconds):
    connection = socket.create_connection((host, port), timeout=10)
    connection.settimeout(None)
    end = time.time() + seconds

    # Keep sending for the whole time. If the network breaks, sendall() just waits
    # until TCP gets through again - the receiver's log shows how long that took.
    while time.time() < end:
        connection.sendall(BLOCK)

    connection.close()


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit("usage: receiver HOST PORT OUT.csv | sender HOST PORT SECONDS")
    if sys.argv[1] == "receiver":
        receiver(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        sender(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
