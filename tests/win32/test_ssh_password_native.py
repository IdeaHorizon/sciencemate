"""Native executable and opt-in OpenSSH password round trip on disposable hosts."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import ssh_transport

PASSWORD = "test-only-密码 $ & \" '"


@unittest.skipUnless(sys.platform == "win32", "native Windows required")
class WindowsSSHPassword(unittest.TestCase):
    def test_native_helper_writes_exact_utf8_without_shell_interpretation(self):
        with tempfile.TemporaryDirectory(prefix="hf-askpass-test-") as temporary:
            helper = ssh_transport._windows_askpass(Path(temporary))
            done = subprocess.run([str(helper)], capture_output=True, check=True,
                                  env={**os.environ, "HF_SSH_PASSWORD": PASSWORD})
            self.assertEqual(done.stdout, PASSWORD.encode("utf-8"))
            self.assertEqual(done.stderr, b"")

    @unittest.skipUnless(os.getenv("HARNESS_TEST_SSH_HOST"), "disposable SSH server required")
    def test_real_openssh_authenticates_with_native_askpass(self):
        original = subprocess.Popen
        with tempfile.TemporaryDirectory(prefix="hf-ssh-hostkeys-test-") as temporary:
            known_hosts = str(Path(temporary) / "known_hosts")

            def launch(argv, **kwargs):
                if argv[0] == "ssh":
                    self.assertNotIn(PASSWORD, json.dumps(argv))
                    argv = [argv[0], "-o", f"UserKnownHostsFile={known_hosts}", *argv[1:]]
                return original(argv, **kwargs)

            connection = ssh_transport.SSHConnection(
                os.environ["HARNESS_TEST_SSH_HOST"], user="backlog-test",
                port=int(os.environ["HARNESS_TEST_SSH_PORT"]),
            )
            with patch.object(subprocess, "Popen", launch):
                result = ssh_transport.SSHTransport(connection, PASSWORD).call({"action": "probe"})
            self.assertEqual(result, {"status": "success", "authenticated": True})
            self.assertTrue(Path(known_hosts).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
