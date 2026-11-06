# ============================================================
# TCP BACKUP SYSTEM
# CHECKSUM / FILE INTEGRITY MODULE
# ============================================================

import hashlib


# ============================================================
# CALCULATE SHA-256 HASH OF A FILE
# ============================================================

def calculate_sha256(file_path):

    # Create a SHA-256 hash object.
    sha256 = hashlib.sha256()

    # Open the file in binary read mode.
    with open(file_path, "rb") as file:

        while True:

            # Read the file in blocks.
            data = file.read(65536)

            # End of file.
            if not data:
                break

            # Add the data to the hash.
            sha256.update(data)

    # Return the final hash as a hexadecimal string.
    return sha256.hexdigest()


# ============================================================
# CALCULATE SHA-256 HASH OF DATA IN MEMORY (one chunk)
# ============================================================

def calculate_sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()
