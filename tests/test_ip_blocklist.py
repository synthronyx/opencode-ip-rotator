import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import ip_blocklist


class EgressBlocklistTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = mock.patch.object(
            ip_blocklist, "DB_PATH", Path(self.directory.name) / "metrics.db"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ip_blocklist.init_db()

    def test_block_and_lookup(self):
        expiry = ip_blocklist.block_ip("203.0.113.5", time.time() + 3600, "quota")
        self.assertIsNotNone(expiry)
        self.assertTrue(ip_blocklist.is_blocked("203.0.113.5"))
        self.assertEqual(len(ip_blocklist.list_blocked()), 1)

    def test_expired_entries_are_not_blocked(self):
        ip_blocklist.block_ip("203.0.113.6", time.time() - 1, "quota")
        self.assertFalse(ip_blocklist.is_blocked("203.0.113.6"))
        self.assertEqual(ip_blocklist.list_blocked(), [])

    def test_block_never_shortens_an_existing_window(self):
        ip_blocklist.block_ip("203.0.113.7", time.time() + 7200, "quota")
        ip_blocklist.block_ip("203.0.113.7", time.time() + 60, "quota again")
        remaining = ip_blocklist.blocked_until("203.0.113.7") - time.time()
        self.assertGreater(remaining, 7000)

    def test_prune_removes_only_expired_entries(self):
        ip_blocklist.block_ip("203.0.113.8", time.time() - 1)
        ip_blocklist.block_ip("203.0.113.9", time.time() + 60)
        self.assertEqual(ip_blocklist.prune(), 1)
        self.assertEqual(
            [entry["ip"] for entry in ip_blocklist.list_blocked()], ["203.0.113.9"]
        )

    def test_missing_ip_is_ignored(self):
        self.assertIsNone(ip_blocklist.block_ip(None, time.time() + 60))
        self.assertFalse(ip_blocklist.is_blocked(None))
        self.assertIsNone(ip_blocklist.blocked_until(""))

    def test_ipv6_uses_upstream_64_prefix(self):
        self.assertEqual(
            ip_blocklist.quota_identity("2a09:bac5:3f10:a82::10c:72"),
            "2a09:bac5:3f10:a82",
        )
        self.assertEqual(
            ip_blocklist.quota_identity("203.0.113.5"), "203.0.113.5"
        )

    def test_blocking_one_ipv6_address_blocks_its_64(self):
        ip_blocklist.block_ip("2a09:bac5:3f10:a82::10c:72", time.time() + 3600)
        self.assertTrue(
            ip_blocklist.is_blocked("2a09:bac5:3f10:a82::10c:76")
        )
        self.assertFalse(
            ip_blocklist.is_blocked("2a09:bac5:3f11:a82::10c:76")
        )
        self.assertEqual(
            [entry["ip"] for entry in ip_blocklist.list_blocked()],
            ["2a09:bac5:3f10:a82"],
        )

    def test_init_db_migrates_full_ipv6_rows(self):
        with ip_blocklist._connect() as conn:
            conn.execute(
                "INSERT INTO blocked_egress_ips (ip, blocked_until, reason, created_at) VALUES (?, ?, ?, ?)",
                (
                    "2a09:bac1:36c0:5f70::2a6:55",
                    time.time() + 3600,
                    "quota",
                    time.time(),
                ),
            )
        ip_blocklist.init_db()
        self.assertTrue(
            ip_blocklist.is_blocked("2a09:bac1:36c0:5f70::2a8:16")
        )
        self.assertEqual(
            [entry["ip"] for entry in ip_blocklist.list_blocked()],
            ["2a09:bac1:36c0:5f70"],
        )


if __name__ == "__main__":
    unittest.main()
