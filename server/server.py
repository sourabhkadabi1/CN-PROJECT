import socket
import os
import sys
import json
import shutil
import threading

from protocol.protocol import (
    MAX_CHUNK_SIZE,
    MAX_FILENAME_SIZE,
    SHA256_LENGTH,
    MSG_BACKUP_START,
    MSG_RESUME_QUERY,
    MSG_CHUNK,
    MSG_BACKUP_DONE,
    REPLY_START_OK,
    REPLY_RESUME,
    REPLY_CHUNK_OK,
    REPLY_CHUNK_BAD,
    REPLY_DONE_OK,
    REPLY_DONE_FAILED,
    REPLY_ERROR,
    send_line,
    receive_line,
    receive_exact,
)
from utils.checksum import calculate_sha256, calculate_sha256_bytes


# ============================================================
# COMMAND-LINE ARGUMENTS
# ============================================================

if len(sys.argv) > 4:
    print("Usage: python -m server.server [bind_ip] [port] [storage_dir]")
    print("Examples:")
    print("  python -m server.server")
    print("  python -m server.server 0.0.0.0 5000 backup/h2")
    sys.exit(1)

SERVER_IP = sys.argv[1] if len(sys.argv) >= 2 else "0.0.0.0"
STORAGE_FOLDER = sys.argv[3] if len(sys.argv) >= 4 else "backup/primary"

try:
    SERVER_PORT = int(sys.argv[2]) if len(sys.argv) >= 3 else 5000
except ValueError:
    print("ERROR: Port must be a valid number.")
    sys.exit(1)

os.makedirs(STORAGE_FOLDER, exist_ok=True)

print_lock = threading.Lock()


def say(peer, message):
    # Several clients are served at once, so keep each printed line whole.
    with print_lock:
        print(f"[{peer}] {message}", flush=True)


# ============================================================
# BACKUP RECORD  =  everything stored about ONE backed-up file
#
#   <storage>/<file sha256>/manifest.json          which chunks are safely stored
#   <storage>/<file sha256>/chunk_000000.chunk     one file per chunk
#   <storage>/<filename>                           the rebuilt file (after verification)
# ============================================================

class BackupRecord:

    def __init__(self, file_hash):
        self.folder = os.path.join(STORAGE_FOLDER, file_hash)
        self.manifest_path = os.path.join(self.folder, "manifest.json")
        self.lock = threading.Lock()      # protects the manifest and the chunk files
        self.manifest = None

        # A server restart (or crash) must not lose track of stored chunks.
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, "r") as file:
                self.manifest = json.load(file)

    def chunk_path(self, index):
        return os.path.join(self.folder, f"chunk_{index:06d}.chunk")

    def save_manifest(self):
        # Write to a .part file first and rename, exactly like a finished backup:
        # a crash can never leave a half-written manifest.
        part_path = self.manifest_path + ".part"
        with open(part_path, "w") as file:
            json.dump(self.manifest, file)
        os.replace(part_path, self.manifest_path)

    # --------------------------------------------------------
    # BACKUP_START
    # --------------------------------------------------------
    def start(self, filename, file_size, chunk_size, chunk_count, restart):
        with self.lock:
            if restart and os.path.isdir(self.folder):
                shutil.rmtree(self.folder)
                self.manifest = None

            os.makedirs(self.folder, exist_ok=True)

            if self.manifest is None:
                self.manifest = {
                    "filename": filename,
                    "file_size": file_size,
                    "chunk_size": chunk_size,
                    "chunk_count": chunk_count,
                    "chunks": {},          # chunk index -> SHA-256 of that chunk
                }
                self.save_manifest()
            elif (self.manifest["file_size"] != file_size
                  or self.manifest["chunk_size"] != chunk_size
                  or self.manifest["chunk_count"] != chunk_count):
                raise ValueError("Same file hash but different file parameters.")

            return len(self.manifest["chunks"])

    # --------------------------------------------------------
    # RESUME_QUERY  -> which chunks are really stored?
    # --------------------------------------------------------
    def held_chunks(self):
        with self.lock:
            held = []
            for key in sorted(self.manifest["chunks"], key=int):
                index = int(key)
                try:
                    with open(self.chunk_path(index), "rb") as file:
                        good = calculate_sha256_bytes(file.read()) == self.manifest["chunks"][key]
                except OSError:
                    good = False

                if good:
                    held.append(index)
                else:
                    # Missing or damaged on disk: forget it, the client will resend it.
                    del self.manifest["chunks"][key]

            self.save_manifest()
            return held

    # --------------------------------------------------------
    # CHUNK  -> store one verified chunk
    # --------------------------------------------------------
    def store_chunk(self, index, data, chunk_hash):
        with self.lock:
            final_path = self.chunk_path(index)
            part_path = final_path + ".part"

            with open(part_path, "wb") as file:
                file.write(data)

            # Only now does the chunk become a real stored chunk
            os.replace(part_path, final_path)

            self.manifest["chunks"][str(index)] = chunk_hash
            self.save_manifest()

    # --------------------------------------------------------
    # BACKUP_DONE  -> check, rebuild and verify the whole file
    # --------------------------------------------------------
    def rebuild(self, file_hash, filename):
        with self.lock:
            count = self.manifest["chunk_count"]

            missing = [i for i in range(count) if str(i) not in self.manifest["chunks"]]
            if missing:
                return False, missing, []

            # 1) Re-check every stored chunk
            bad = []
            for index in range(count):
                try:
                    with open(self.chunk_path(index), "rb") as file:
                        good = (calculate_sha256_bytes(file.read())
                                == self.manifest["chunks"][str(index)])
                except OSError:
                    good = False
                if not good:
                    bad.append(index)

            if bad:
                for index in bad:
                    self.manifest["chunks"].pop(str(index), None)
                self.save_manifest()
                return False, [], bad

            # 2) Join the chunks into one file (still a .part file)
            final_path = os.path.join(STORAGE_FOLDER, filename)
            part_path = final_path + ".part"

            with open(part_path, "wb") as out_file:
                for index in range(count):
                    with open(self.chunk_path(index), "rb") as chunk_file:
                        out_file.write(chunk_file.read())

            # 3) Compare with the hash of the ORIGINAL file
            actual_hash = calculate_sha256(part_path)
            if actual_hash != file_hash:
                os.remove(part_path)
                return False, [], []

            os.replace(part_path, final_path)
            return True, [], []


records = {}
records_lock = threading.Lock()


def get_record(file_hash):
    # Every client backing up the same file shares one record.
    with records_lock:
        if file_hash not in records:
            records[file_hash] = BackupRecord(file_hash)
        return records[file_hash]


# ============================================================
# VALIDATE A BACKUP_START MESSAGE
# ============================================================

def parse_start(fields):
    if len(fields) != 7:
        raise ValueError("Invalid header format.")

    filename = fields[1]
    file_size = int(fields[2])
    chunk_size = int(fields[3])
    chunk_count = int(fields[4])
    file_hash = fields[5].lower()
    restart = fields[6] == "1"

    if len(filename.encode("utf-8")) == 0:
        raise ValueError("Filename cannot be empty.")
    if len(filename.encode("utf-8")) > MAX_FILENAME_SIZE:
        raise ValueError("Filename is too long.")
    if file_size < 0:
        raise ValueError("Invalid file size.")
    if chunk_size < 1 or chunk_size > MAX_CHUNK_SIZE:
        raise ValueError("Invalid chunk size.")
    if chunk_count != max(1, -(-file_size // chunk_size)):
        raise ValueError("Chunk count does not match file size.")
    if len(file_hash) != SHA256_LENGTH:
        raise ValueError("Invalid SHA-256 length.")
    try:
        int(file_hash, 16)
    except ValueError:
        raise ValueError("Invalid SHA-256 value.")

    # Prevent path traversal
    filename = os.path.basename(filename)
    if not filename:
        raise ValueError("Invalid filename.")

    return filename, file_size, chunk_size, chunk_count, file_hash, restart


# ============================================================
# HANDLE ONE CLIENT  (runs in its own thread)
# ============================================================

def handle_client(connection, address):
    peer = f"{address[0]}:{address[1]}"
    record = None
    filename = None
    file_hash = None

    try:
        connection.settimeout(30)
        say(peer, "Client connected")

        while True:
            try:
                fields = receive_line(connection)
            except socket.timeout:
                say(peer, "Client idle for 30 seconds - closing")
                return

            command = fields[0]

            # ---------------------------------------------------- BACKUP_START
            if command == MSG_BACKUP_START:
                (filename, file_size, chunk_size, chunk_count,
                 file_hash, restart) = parse_start(fields)

                record = get_record(file_hash)
                already_stored = record.start(filename, file_size, chunk_size,
                                              chunk_count, restart)

                say(peer, f"Backup of '{filename}': {file_size} bytes, "
                          f"{chunk_count} chunks ({already_stored} already stored)")
                send_line(connection, REPLY_START_OK)

            elif record is None:
                raise ValueError("BACKUP_START must come first.")

            # ---------------------------------------------------- RESUME_QUERY
            elif command == MSG_RESUME_QUERY:
                held = record.held_chunks()
                say(peer, f"Resume query: {len(held)} chunks are stored")
                send_line(connection, REPLY_RESUME, ",".join(str(i) for i in held))

            # ---------------------------------------------------- CHUNK
            elif command == MSG_CHUNK:
                if len(fields) != 4:
                    raise ValueError("Invalid chunk header.")

                index = int(fields[1])
                length = int(fields[2])
                chunk_hash = fields[3].lower()

                manifest = record.manifest
                if index < 0 or index >= manifest["chunk_count"]:
                    raise ValueError("Chunk index out of range.")

                expected_length = min(manifest["chunk_size"],
                                      manifest["file_size"] - index * manifest["chunk_size"])
                if length != expected_length:
                    raise ValueError("Chunk length is wrong.")

                # Always read the data, so the stream stays in step.
                data = receive_exact(connection, length)

                if calculate_sha256_bytes(data) == chunk_hash:
                    record.store_chunk(index, data, chunk_hash)
                    send_line(connection, REPLY_CHUNK_OK, index)
                else:
                    say(peer, f"✗ Chunk {index} damaged in transit - asking for a resend")
                    send_line(connection, REPLY_CHUNK_BAD, index)

            # ---------------------------------------------------- BACKUP_DONE
            elif command == MSG_BACKUP_DONE:
                ok, missing, bad = record.rebuild(file_hash, filename)

                if ok:
                    say(peer, "✓ INTEGRITY VERIFIED")
                    say(peer, f"✓ BACKUP STORED: {os.path.join(STORAGE_FOLDER, filename)}")
                    send_line(connection, REPLY_DONE_OK)
                else:
                    say(peer, f"✗ Verification failed (missing={missing}, bad={bad})")
                    send_line(connection, REPLY_DONE_FAILED,
                              "missing=" + ",".join(str(i) for i in missing),
                              "bad=" + ",".join(str(i) for i in bad))

            else:
                raise ValueError(f"Unknown message '{command}'.")

    except ValueError as error:
        say(peer, f"ERROR: Invalid request: {error}")
        try:
            send_line(connection, REPLY_ERROR, error)
        except OSError:
            pass

    except ConnectionError as error:
        # Stored chunks stay on disk, so the client can resume later.
        say(peer, f"ERROR: Connection problem: {error}")

    except socket.timeout:
        say(peer, "ERROR: Client timed out.")

    except OSError as error:
        say(peer, f"ERROR: File/Network error: {error}")

    finally:
        connection.close()
        say(peer, "Client connection closed.")


# ============================================================
# MAIN SERVER LOOP
# ============================================================

def main():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((SERVER_IP, SERVER_PORT))
        server_socket.listen(20)

        print("=" * 60)
        print("TCP BACKUP SERVER (chunked, resumable)")
        print("=" * 60)
        print(f"Listening on : {SERVER_IP}:{SERVER_PORT}")
        print(f"Storage      : {STORAGE_FOLDER}")
        print("\nServer is waiting for clients...\n")

        while True:
            connection, address = server_socket.accept()

            # One thread per client, so several clients can back up at the same time.
            thread = threading.Thread(target=handle_client,
                                      args=(connection, address), daemon=True)
            thread.start()

    except OSError as error:
        print(f"ERROR: Could not start server: {error}")
    except KeyboardInterrupt:
        print("\n\nServer stopped by user.")
    finally:
        server_socket.close()
        print("Server socket closed.")


if __name__ == "__main__":
    main()
