# Component notices and distribution scope

This project contains new orchestration/adapters and selected source from the existing Hongguo project. Component terms remain separate. This file does not relicense upstream code, models, binaries, video or branding assets and does not assert that every component is MIT.

| Component inspected | Actual local evidence | License label / scope |
|---|---|---|
| VideoCaptioner | Upstream commit `95842ecb5618c0b6a548a336bdfb0eb859bdb501`, upstream `LICENSE` and `pyproject.toml`; exact license copy in `licenses/VideoCaptioner-GPL-3.0.txt` | GNU GPL v3; project metadata says GPL-3.0. Preserve corresponding source/notices when distributing covered code |
| Faster-Whisper 1.1.1 | Installed distribution `LICENSE`, copied to `licenses/faster-whisper-1.1.1-MIT.txt` | MIT, copyright 2023 SYSTRAN |
| Hongguo source adapter | Vendored `package.json` (`GPL-3.0-only`), `LICENSE`, and `licenses/LICENSE.drpys.txt`; copies under `licenses/` | GNU GPL v3. This is not an MIT adapter |
| Hongguo 3.1.0 core | Selected source/tools/config/asset bytes copied into `vendor/core` or `assets`; hashes are recorded by release manifest | No blanket root LICENSE was found in the inspected core project. Its source-adapter terms do not automatically label every original Hongguo file. No new blanket license is asserted here |
| MVSEP-CDX23 | Upstream revision `aaa75640d8fd68418948fe4cd2c2d263d042cbb9`, actual `MVSEP-CDX23-LICENSE.txt` | MIT repository code, copyright 2026 Roman Solovyev (ZFTurbo). External checkpoint is referenced with exact hash and upstream acquisition, not bundled |
| Demucs | `licenses/Demucs-LICENSE.txt` | MIT; external installed dependency |
| Kokoro and Misaki 0.9.4 | Installed license texts copied byte-for-byte | Apache-2.0 |
| Kokoro-82M / voice af_heart | Model manifest revision `f3ff3571791e39611d31c381e3a41a3af07b4987`; `Kokoro-model-Apache-2.0.txt` | Apache-2.0 model/voice metadata; payloads not bundled |
| Phonemizer fork / eSpeak NG | GPL license texts included; espeakng-loader upstream MIT text included | Keep the separate GPL and MIT notices; the exact inventory of the eSpeak binary embedded by espeakng-loader has not been completed |
| Torch / spaCy / English model / Pillow | Included exact license/notice copies and TTS provenance inventory | See each component text; do not replace them with a single project label |

The default ASR adapter invokes the real pinned VideoCaptioner JianYingASR cloud client. New wrappers add bounded transport/polling, raw response/provenance retention, redacted release evidence and explicit segmentation/frame lineage. Server model/weights are not available for local hash locking and are not claimed as pinned. The retained optional local ASR adapter subclasses VideoCaptioner and calls the Faster-Whisper Python API; its old GUI/progress bridge is replaced explicitly. Upstream VideoCaptioner files are not edited. New controller/media adapters integrate selected legacy functions; release source includes these adapters and the vendored code they execute. These are material modifications in behavior, not a claim that stock VideoCaptioner performs all new workflow stages.

The Jianying subclass explicitly corrects a pinned-client upload protocol defect found in a preserved failed smoke: an extra `_upload_commit` PUT returned HTTP 400 after successful upload and final POST/checksum acknowledgement. The adapter retains the actual upstream sign/auth/file/check methods, requires successful final acknowledgement with exact CRC32, and omits that redundant PUT; submit/query must still prove the uploaded object usable. This is a documented adapter modification, not an edit to the upstream checkout or a claim that a blocked service was bypassed.

`config/generated/videocaptioner-version.py` is an exact copy of the observed generated VideoCaptioner version payload, supplied because the pinned Git commit does not track `_version.py`. Setup copies it only into a newly provisioned checkout that lacks the generated file; the original checkout is preserved. The expected SHA256 is part of the client code lock, and the actual request header version is checked against the locked client version before HTTP transport.

Runtime/model payloads, CUDA/FFmpeg binaries, source videos and the complete package wheel archive are excluded from this source release unless a release manifest explicitly lists them. The supplied license texts are attribution/provenance evidence; they are not proof that a separate binary redistribution bundle has been audited. Existing internal dependency manifests include installed RECORD hashes, which are not downloadable wheel hashes.

The original logo and wordmark are copied byte-for-byte for the requested workflow; their software licensing is not inferred. Software licenses do not establish rights to distribute source films or branding. No public upload or publication is performed by `package`.

License texts in `licenses/` were read from the existing local checkout/distributions. Their SHA256 values and source-relative names are recorded in `config/dependencies.json`; the release manifest additionally hashes the actual files shipped. Retain these notices with corresponding redistributed source/components.
