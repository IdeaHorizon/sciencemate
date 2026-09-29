"""Execute the real self-extractor, including an Authenticode-signed overlay.

Uses an ephemeral, untrusted test certificate; never modifies a trust store or
the user's installed application. Run this on native Windows, not WSL Python.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows installer")
ROOT = Path(__file__).resolve().parents[1]
LONG_REPORT = Path(*(["nested-research-data-" * 2] * 6), "result.txt")


@pytest.fixture
def installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("native_installer_builder", ROOT / "scripts/package/build_windows_app.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    output = tmp_path / "dist"
    output.mkdir()
    monkeypatch.setattr(builder, "DIST", output)
    app = tmp_path / "app"
    app.mkdir()
    (app / "ScienceMate.exe").write_bytes(b"isolated installer fixture")
    (app / "backend.json").write_bytes(b"{}")
    (app / "report.txt").write_bytes("研究数据\n".encode("utf-8"))
    from shared.lib.filesystem import io_path
    report = io_path(app / LONG_REPORT)
    report.parent.mkdir(parents=True)
    report.write_bytes(b"long path exact bytes\r\n")
    return builder.make_the_installer(app)


def run_installer(installer, destination):
    return subprocess.run([str(installer), "--no-launch", "--install-dir", str(destination)],
                          capture_output=True, timeout=60)


def test_unsigned_installer_and_reinstall_preserve_exact_payload(installer, tmp_path):
    destination = tmp_path / "installed"
    for _ in range(2):
        result = run_installer(installer, destination)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert (destination / "report.txt").read_bytes() == "研究数据\n".encode("utf-8")
        from shared.lib.filesystem import io_path
        report = io_path(destination / LONG_REPORT)
        assert len(str(report)) > 260
        assert report.read_bytes() == b"long path exact bytes\r\n"
    assert not list(tmp_path.glob("installed.old-*"))
    assert not list(tmp_path.glob("installed.new-*"))


def test_installer_refuses_an_unrelated_directory(installer, tmp_path):
    destination = tmp_path / "my-files"
    destination.mkdir()
    precious = destination / "keep.txt"
    precious.write_bytes(b"user-owned")
    result = run_installer(installer, destination)
    assert result.returncode != 0
    assert precious.read_bytes() == b"user-owned"
    assert list(destination.iterdir()) == [precious]


def test_authenticode_certificate_does_not_hide_the_payload(installer, tmp_path):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = tmp_path / "sign-test-installer.ps1"
    script.write_text(r'''param([string]$Installer)
$ErrorActionPreference = "Stop"
$certificate = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=ScienceMate isolated installer test" -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddDays(1)
try {
    $signature = Set-AuthenticodeSignature -FilePath $Installer -Certificate $certificate -HashAlgorithm SHA256
    if (-not $signature.SignerCertificate) { throw "No Authenticode certificate was written" }
} finally {
    Remove-Item ("Cert:\CurrentUser\My\" + $certificate.Thumbprint) -DeleteKey
}
''', encoding="utf-8")
    before = installer.stat().st_size
    signed = subprocess.run([str(powershell), "-NoProfile", "-File", str(script), str(installer)],
                            capture_output=True, timeout=60)
    assert signed.returncode == 0, (signed.stdout, signed.stderr)
    assert installer.stat().st_size > before
    destination = tmp_path / "signed-install"
    result = run_installer(installer, destination)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (destination / "report.txt").read_bytes() == "研究数据\n".encode("utf-8")

    from shared.lib.filesystem import io_path
    assert io_path(destination / LONG_REPORT).read_bytes() == b"long path exact bytes\r\n"


@pytest.mark.parametrize("entry", ["../escape.txt", "C:/escape.txt", "file.txt:stream", "child/../../escape.txt"])
def test_invalid_package_entry_preserves_existing_install_and_cleans_staging(installer, tmp_path, entry):
    import io
    import struct
    import zipfile
    data = installer.read_bytes()
    offset, magic = struct.unpack("<qq", data[-16:])
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("ScienceMate.exe", b"new application")
        archive.writestr(entry, b"must never escape staging")
    unsigned = data[:offset] + payload.getvalue()
    unsigned += b"\0" * (-len(unsigned) % 8)
    installer.write_bytes(unsigned + struct.pack("<qq", offset, magic))
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "ScienceMate.exe").write_bytes(b"old application")
    (destination / "backend.json").write_bytes(b"{}")
    result = run_installer(installer, destination)
    assert result.returncode != 0
    assert (destination / "ScienceMate.exe").read_bytes() == b"old application"
    assert not list(tmp_path.glob("existing.new-*"))
    assert not list(tmp_path.glob("existing.old-*"))
    assert not (tmp_path / "escape.txt").exists()
