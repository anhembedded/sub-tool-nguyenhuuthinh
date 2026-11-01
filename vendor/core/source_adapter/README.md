# Exact HEVC1080 source adapter

This isolated source adapter handles Hongguo App request signing, Spade parsing and source
transport implementation. It requires exact **1920×1080 HEVC and 30 fps** after
FFprobe and complete FFmpeg decode. It never transcodes or substitutes bytevc2,
H264, another resolution, or web media. Website access supplies episode IDs only.

The adapter has offline selector, metadata, range, protected-path and lock tests.
An online download is not validated by those tests.
Remote App protocol availability remains an external runtime dependency.

## Use

Requires Node.js >=22.12 and FFmpeg/FFprobe on PATH, or explicit executable paths.
The package contains the locally installed axios1.20.0 dependency closure for
offline execution. `package-lock.json` pins the dependency installation. An
online installation can use `npm ci --ignore-scripts` if `node_modules` is removed.
Node, FFmpeg/FFprobe and the pinned dependency closure are runtime requirements.

```powershell
node cli.cjs --help
npm test
node cli.cjs validate --source "C:\path\ep01_app_1080p.mp4" --report "./qa/local_source.json"
node cli.cjs download --config config.example.json
node cli.cjs download --series-id VERIFIED_SERIES_ID --episodes 1-80 --output-root "./runtime/new-run-sources"
```

Download writes only `outputRoot/sources/` and `outputRoot/download_report.json`.
An exclusive output lock prevents two runs from using the same root. A completed
source receives an identity sidecar with episode number, series ID, video ID and
SHA256. Existing sources require matching identity and SHA256 plus full decode
before reuse. Unverified or mismatched existing files are preserved and rejected.
Use a fresh output directory when importing prior production files.

`protected-paths.json` freezes the original media/cache and approved output roots.
Download rejects an output root equal to, inside, or above any of those roots,
including when the Node CLI is invoked directly. Existing filesystem ancestors
are resolved before checking, covering path aliases/junctions. The same guard
runs before any download directory or report is created. The marker uses a
cryptographically random token and original file metadata; cleanup verifies both
before removing it. Missing, replaced, modified or stale ambiguous locks require
manual inspection and are never automatically deleted.

All sources must have one video stream, source audio, positive duration, positive
frame count and exact 30/1 nominal/average metadata. This stream metadata gate is
not proof of CFR at every packet; canonical frame/PTS QA remains required by the
downstream timeline. Decoding never changes source audio gain or subtitles.

Download stops scheduling useful new sources after401/403/429 or business/security
rejection. Already in-flight bounded requests may finish. Temporary encrypted or
decoded files from a failed run remain inside the isolated output root, and only
the same adapter-owned temporary names are cleared on resume. Do not run two
independent download roots into a common sources directory.

No signed CDN URL, content key, authorization token, API body or request signature
is serialized in reports. Media errors are sanitized before logging. Content
keys are transient native-decoder arguments; do not enable raw process argument
logging when invoking this adapter.

The read-only `validate` command writes only a caller-requested report and rejects
a report path identical to its source. Use an isolated validation directory for
reports; validation is not a production source import operation.

## Provenance

This component is **GPL-3.0-only**, not MIT. `LICENSE`, `licenses/` and
`PROVENANCE.json` retain source/license records. axios is MIT with transitive
licenses retained in its vendored package tree. FFmpeg binaries are not bundled.
The integration workflow and current validation scope are in `../docs/FRESH_RUN.md`.
