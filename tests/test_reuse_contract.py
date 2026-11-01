"""Explicit cross-run adoption must meet the same identity/hash rules as cache."""
from pathlib import Path
import tempfile
import unittest

from hongguo_lite.common import sha
from hongguo_lite.store import Store


class CrossRunReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.inputs = {"source": "source-sha", "engine": "locked-mvsep-v1", "config": {"precision": "FP32"}}
        self.old_store = Store(self.old)
        def original(out):
            path = out / "background.bin"
            path.write_bytes(b"original verified samples")
            return {"output": str(path), "files": [str(path)]}
        self.result = self.old_store.stage("ep01/separation", self.inputs, original)
        self.old_receipt = Path(self.old_store.state["stages"]["ep01/separation"]["receipt"])
        self.old_receipt_sha = sha(self.old_receipt)
        self.calls = []

    def rebuild(self, out):
        self.calls.append(out)
        path = out / "background.bin"
        path.write_bytes(b"newly generated verified samples")
        return {"output": str(path), "files": [str(path)]}

    def test_exact_reuse_adopts_verified_artifact_without_modifying_prior_receipt(self):
        store = Store(self.new, self.old)
        adopted = store.stage("ep01/separation", self.inputs, self.rebuild)
        self.assertEqual(self.calls, [])
        self.assertEqual(adopted["output"], self.result["output"])
        self.assertEqual(adopted["reuse_provenance"]["receipt_sha256"], self.old_receipt_sha)
        self.assertEqual(sha(self.old_receipt), self.old_receipt_sha)
        self.assertEqual(store.stats["adopted"], 1)

    def test_changed_engine_never_adopts_old_background(self):
        store = Store(self.new, self.old)
        result = store.stage("ep01/separation", {**self.inputs, "engine": "locked-mvsep-v2"}, self.rebuild)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("reuse_provenance", result)
        self.assertEqual(store.stats["adopted"], 0)
        self.assertEqual(sha(self.old_receipt), self.old_receipt_sha)

    def test_changed_artifact_hash_never_adopts_even_with_same_input_signature(self):
        Path(self.result["output"]).write_bytes(b"tampered original samples")
        store = Store(self.new, self.old)
        result = store.stage("ep01/separation", self.inputs, self.rebuild)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(result["output"], self.result["output"])
        self.assertEqual(store.stats["adopted"], 0)

    def test_changed_prior_receipt_hash_never_adopts(self):
        self.old_receipt.write_bytes(self.old_receipt.read_bytes() + b" ")
        store = Store(self.new, self.old)
        store.stage("ep01/separation", self.inputs, self.rebuild)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(store.stats["adopted"], 0)


if __name__ == "__main__":
    unittest.main()
