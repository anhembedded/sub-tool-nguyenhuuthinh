import io
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import os
import unittest
from unittest.mock import patch, MagicMock
import zipfile

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / 'tools'))
import runtime_setup as setup


class ProvisionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows CUDA driver API')
    def test_driver_probe_rejects_no_device_without_any_download(self):
        library = MagicMock(); library.cuInit.return_value = 0
        library.cuDeviceGetCount.return_value = 0
        with patch.object(setup.ctypes, 'WinDLL', return_value=library), patch.object(setup, 'download') as transfer:
            with self.assertRaisesRegex(RuntimeError, 'No CPU fallback'): setup.nvidia_driver()
            transfer.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell launcher')
    def test_powershell_preserves_double_dash_flags_and_space_paths(self):
        with tempfile.TemporaryDirectory(prefix='workflow launcher ') as directory:
            root = Path(directory); configuration = root / 'launcher config.json'
            configuration.write_text(json.dumps({'env': {'controller_python': sys.executable}}))
            missing = root / 'absent run'
            process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'workflow.ps1'),
                'status', '-Config', str(configuration), '--run', str(missing)], capture_output=True, text=True, timeout=90)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('FileNotFoundError', process.stderr)
            self.assertIn('absent run', process.stderr)
            process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'workflow.ps1'),
                'status', '--config', str(configuration), '--help'], capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn('--run', process.stdout)

    def test_verified_download_preserves_bad_bytes_and_never_uses_them(self):
        class Response(io.BytesIO): url = 'https://example.test/artifact'
        with tempfile.TemporaryDirectory() as directory:
            artifact = {'url': Response.url, 'filename': 'artifact', 'sha256': '0' * 64}
            with patch.object(setup.urllib.request, 'urlopen', return_value=Response(b'wrong')):
                with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                    setup.download(artifact, directory)
            self.assertFalse((Path(directory) / 'artifact').exists())
            self.assertEqual(len(list(Path(directory).glob('*.partial'))), 1)

    def test_cached_download_is_content_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'artifact'; target.write_bytes(b'approved')
            artifact = {'url': 'https://example.test/a', 'filename': target.name, 'sha256': setup.sha(target)}
            self.assertEqual(setup.download(artifact, directory), target)
            target.write_bytes(b'corrupt!')
            with self.assertRaisesRegex(ValueError, 'changed'): setup.download(artifact, directory)

    def test_archive_cannot_escape_private_runtime_and_reuse_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'archive.zip'
            with zipfile.ZipFile(archive, 'w') as package: package.writestr('../escape', b'bad')
            with self.assertRaisesRegex(ValueError, 'escapes'): setup.extract_zip(archive, root / 'target')
            self.assertFalse((root / 'escape').exists())
            with zipfile.ZipFile(archive, 'w') as package: package.writestr('bin/tool.exe', b'approved')
            setup.extract_zip(archive, root / 'target'); setup.verify_zip(archive, root / 'target')
            (root / 'target/bin/tool.exe').write_bytes(b'modified')
            with self.assertRaisesRegex(ValueError, 'changed'): setup.verify_zip(archive, root / 'target')

    def test_requirements_lock_complete_archives_have_exact_hashes(self):
        for role in setup.ROLES:
            document = json.loads((ROOT / 'config/locks' / (role + '.archives.json')).read_text())
            text = (ROOT / 'config/locks' / (role + '.requirements.txt')).read_text()
            self.assertTrue(document['packages'])
            for row in document['packages']:
                self.assertTrue(row['url'].startswith('https://'))
                self.assertEqual(len(row['sha256']), 64)
                self.assertIn('--hash=sha256:' + row['sha256'], text)

    def test_runtime_identity_detects_new_or_changed_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'module.py'; path.write_text('value = 1')
            before = setup.role_files(root); path.write_text('value = 2')
            self.assertNotEqual(before, setup.role_files(root))
            path.write_text('value = 1'); (root / 'extra.pth').write_text('malicious')
            self.assertNotEqual(before, setup.role_files(root))


if __name__ == '__main__': unittest.main()
