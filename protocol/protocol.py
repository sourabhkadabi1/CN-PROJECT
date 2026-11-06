# ============================================================
# TCP BACKUP SYSTEM
# PROTOCOL MODULE  (shared by the client and the server)
# ============================================================
#
# Every message is ONE line of text:   FIELD|FIELD|FIELD\n
# Only CHUNK is followed by raw bytes (exactly <length> bytes).
#
#   Client sends                                         Server answers
#   ---------------------------------------------------  ----------------------------------------
#   BACKUP_START|name|size|chunk_size|chunk_count|       START_OK
#                file_sha256|restart(0 or 1)
#   RESUME_QUERY                                         RESUME_REPLY|0,1,2,5   (chunks it holds)
#   CHUNK|index|length|chunk_sha256  + <length> bytes    CHUNK_OK|index   or   CHUNK_BAD|index
#   BACKUP_DONE                                          DONE_OK
#                                                        or DONE_FAILED|missing=1,2|bad=7
#   (anything wrong)                                     ERROR|reason     (then connection closes)
#
# file_sha256 is the SHA-256 of the whole original file.
# restart=1 means "forget any chunks you already have for this file".
# ============================================================

# Maximum bytes read from the socket in one recv() call
BUFFER_SIZE = 65536

# Largest chunk the server accepts
MAX_CHUNK_SIZE = 1024 * 1024

# Maximum filename size
MAX_FILENAME_SIZE = 255

# End of header marker
HEADER_SEPARATOR = b"\n"

# Separator between the fields of one line
FIELD_SEPARATOR = "|"

# Maximum length of one header line
MAX_HEADER_SIZE = 1024

# SHA-256 produces 64 hexadecimal characters
SHA256_LENGTH = 64

# Messages: client -> server
MSG_BACKUP_START = "BACKUP_START"
MSG_RESUME_QUERY = "RESUME_QUERY"
MSG_CHUNK = "CHUNK"
MSG_BACKUP_DONE = "BACKUP_DONE"

# Replies: server -> client
REPLY_START_OK = "START_OK"
REPLY_RESUME = "RESUME_REPLY"
REPLY_CHUNK_OK = "CHUNK_OK"
REPLY_CHUNK_BAD = "CHUNK_BAD"
REPLY_DONE_OK = "DONE_OK"
REPLY_DONE_FAILED = "DONE_FAILED"
REPLY_ERROR = "ERROR"


# ============================================================
# SEND / RECEIVE HELPERS
# ============================================================

def send_line(connection, *fields):
    """Send one message line, e.g. send_line(conn, "CHUNK", 3, 65536, hash)."""
    line = FIELD_SEPARATOR.join(str(field) for field in fields)
    connection.sendall(line.encode("utf-8") + HEADER_SEPARATOR)


def receive_line(connection):
    """Read one message line and return its fields as a list of strings."""
    data = b""
    while HEADER_SEPARATOR not in data:
        byte = connection.recv(1)
        if not byte:
            raise ConnectionError("Peer disconnected before sending a complete line.")
        data += byte
        if len(data) > MAX_HEADER_SIZE:
            raise ValueError("Header is too large.")
    line = data.split(HEADER_SEPARATOR, 1)[0].decode("utf-8")
    return line.split(FIELD_SEPARATOR)


def receive_exact(connection, length):
    """Read exactly `length` bytes (TCP may deliver them in several pieces)."""
    data = b""
    while len(data) < length:
        piece = connection.recv(min(BUFFER_SIZE, length - len(data)))
        if not piece:
            raise ConnectionError("Peer disconnected during data transfer.")
        data += piece
    return data
