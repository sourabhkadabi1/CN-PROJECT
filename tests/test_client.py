"""Integration tests for the Member 2 client against the Member 1 server (localhost, no Mininet).

    python3 -m unittest tests.test_client -v
"""
import glob
import hashlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 64 * 1024


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bk_")
        self.procs = []
        self.file = os.path.join(self.tmp, "data.bin")
        with open(self.file, "wb") as f:
            f.write(os.urandom(4 * 1024 * 1024 + 777))       # 65 chunks
        self.sha = sha(self.file)

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_server(self, name, port=None, store=None):
        port = port or free_port()
        store = store or os.path.join(self.tmp, name)
        p = subprocess.Popen([sys.executable, "-m", "server.server", "127.0.0.1",
                              str(port), store], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        return p, port, store

    def client(self, ports, *flags):
        return [sys.executable, "-m", "client.client", self.file] + \
               ["127.0.0.1:%d" % p for p in ports] + list(flags)

    def run_client(self, ports, *flags):
        r = subprocess.run(self.client(ports, *flags), cwd=ROOT, capture_output=True,
                           text=True, timeout=120)
        m = re.search(r"sent: (\d+) \| re-sent: (\d+) \| rejected chunks: (\d+) "
                      r"\| reconnects: (\d+)", r.stdout)
        stats = dict(zip(("sent", "resent", "nacks", "reconnects"),
                         map(int, m.groups()))) if m else {}
        return r.returncode, stats, r.stdout

    def stored(self, store):
        return sha(os.path.join(store, "data.bin")) \
            if os.path.exists(os.path.join(store, "data.bin")) else None


class TestChunkedBackup(Base):
    def test_two_replicas_hash_matches(self):
        _, p1, s1 = self.start_server("a")
        _, p2, s2 = self.start_server("b")
        rc, st, _ = self.run_client([p1, p2])
        self.assertEqual(rc, 0)
        self.assertEqual(st["resent"], 0)
        self.assertEqual(self.stored(s1), self.sha)
        self.assertEqual(self.stored(s2), self.sha)

    def test_corrupted_chunks_are_caught_and_resent(self):
        _, p1, s1 = self.start_server("a")
        rc, st, _ = self.run_client([p1], "--corrupt=0.3")
        self.assertEqual(rc, 0)
        self.assertGreater(st["nacks"], 0)       # server caught them
        self.assertGreater(st["resent"], 0)      # and the client re-sent them
        self.assertEqual(self.stored(s1), self.sha)

    def test_resume_after_connection_drop_sends_only_missing(self):
        _, p1, s1 = self.start_server("a")
        rc, st, out = self.run_client([p1], "--drop-after=40")
        self.assertEqual(rc, 0)
        self.assertEqual(st["reconnects"], 1)
        self.assertIn("server already has 40/65 chunks", out)
        self.assertEqual(st["resent"], 0)        # nothing sent twice
        self.assertEqual(self.stored(s1), self.sha)

    def test_server_killed_mid_backup_then_restarted(self):
        srv, port, store = self.start_server("a")
        cli = subprocess.Popen(self.client([port], "--delay-ms=40"), cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.procs.append(cli)
        time.sleep(1.2)
        srv.send_signal(signal.SIGKILL)          # hard kill, no clean shutdown
        srv.wait()
        time.sleep(1.5)
        self.start_server("a", port=port, store=store)   # same port, same disk
        out, _ = cli.communicate(timeout=90)
        self.assertEqual(cli.returncode, 0)
        self.assertIn("reconnecting", out)
        self.assertIn("server already has", out)
        self.assertEqual(self.stored(store), self.sha)

    def test_damaged_chunk_on_server_disk_is_repaired(self):
        srv, port, store = self.start_server("a")
        # keep a partial backup on disk: drop after 20 chunks then kill the client side
        cli = subprocess.Popen(self.client([port], "--delay-ms=40"), cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(cli)
        time.sleep(1.0)
        cli.kill()
        cli.wait()
        time.sleep(0.5)
        chunks = sorted(glob.glob(os.path.join(store, "*", "chunk_*.chunk")))
        self.assertGreater(len(chunks), 3)
        with open(chunks[2], "r+b") as f:        # silent disk corruption
            f.seek(10)
            f.write(b"\xff\xff\xff\xff")
        rc, st, out = self.run_client([port])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored(store), self.sha)

    def test_no_resume_baseline_resends_everything(self):
        _, p1, s1 = self.start_server("a")
        rc, st, out = self.run_client([p1], "--drop-after=40", "--no-resume")
        self.assertEqual(rc, 0)
        self.assertIn("starting from zero", out)
        self.assertGreaterEqual(st["resent"], 40 * CHUNK)   # the 40 chunks sent twice
        self.assertEqual(self.stored(s1), self.sha)

    def test_dead_server_reported_as_fail_others_still_ok(self):
        _, p1, s1 = self.start_server("a")
        dead = free_port()
        rc, st, out = self.run_client([p1, dead])
        self.assertEqual(rc, 1)
        self.assertIn("1/2 backups verified", out)
        self.assertEqual(self.stored(s1), self.sha)


if __name__ == "__main__":
    unittest.main()
