"""Focused regression tests for deployment CPU-affinity parsing."""

import unittest

from bmv2_switch import resolve_cpu_affinity


class CpuAffinityTests(unittest.TestCase):
    def test_resolves_valid_switch_range(self):
        self.assertEqual(
            resolve_cpu_affinity("s_access_r", "s_core1=0-1,s_access_r=8-9", 12),
            "8,9",
        )

    def test_missing_switch_is_safely_unpinned(self):
        self.assertIsNone(resolve_cpu_affinity("s_core2", "s_core1=0-1", 12))

    def test_undersized_cpu_environment_is_safely_unpinned(self):
        self.assertIsNone(resolve_cpu_affinity("s_access_r", "s_access_r=8-9", 4))

    def test_invalid_range_is_safely_unpinned(self):
        self.assertIsNone(resolve_cpu_affinity("s_access_r", "s_access_r=9-8", 12))


if __name__ == "__main__":
    unittest.main()
