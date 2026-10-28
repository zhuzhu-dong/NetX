"""Regression test for concurrent, same-port UDP workload accounting."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class SharedUdpReceiverTests(unittest.TestCase):
    def test_two_workloads_share_a_udp_port_with_separate_receiver_stats(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            script = Path(__file__).with_name("udp_workload_receiver.py")
            server = subprocess.Popen([
                sys.executable, str(script), "--port", str(port),
                "--stats-dir", temp, "--sample-seconds", "0.1",
            ])
            try:
                time.sleep(0.15)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    for workload_id in ("WL-research", "WL-shared"):
                        for _ in range(40):
                            client.sendto((f"IDN1|{workload_id}|".encode() + b"x" * 100), ("127.0.0.1", port))
                time.sleep(0.25)
                for workload_id in ("WL-research", "WL-shared"):
                    payload = json.loads((Path(temp) / f"idn_workload_{workload_id}.json").read_text())
                    self.assertEqual(payload["status"], "MEASURED")
                    self.assertGreater(payload["protocol_metrics"]["received_packets"], 0)
                    self.assertGreater(payload["protocol_metrics"]["received_mbps"], 0)
            finally:
                server.terminate()
                server.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
