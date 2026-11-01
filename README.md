# NguyenHuuThinh Workflow 1.1.0

Windows x64 workflow for a **new Hongguo whole series**: manual CapCut Chinese captions → contextual English → name OCR → Ava English + **SFX only at −5 dB** → native video and combined SRT.

Supported input: **1920×1080 HEVC, CFR 30fps, SDR yuv420p**. Native frames, audio samples and episode order are retained. Other media profiles are rejected. No source film, translation, glossary, geometry, cache or approval from an earlier film is included.

## Install on another Windows PC

Install CapCut, CPython **3.12 x64** and a compatible NVIDIA driver. Setup downloads its required Microsoft runtime DLLs into a private directory. Internet is required for initial setup, Hongguo source downloads, CapCut recognition and Ava synthesis. CPU fallback is not supported. Unzip into a writable directory and open PowerShell there:

```powershell
powershell -ExecutionPolicy Bypass -File .\workflow.ps1 setup
```

If Python is not detected, provide its full path:

```powershell
.\workflow.ps1 setup -PythonPath 'C:\Path\To\Python312\python.exe'
```

Setup creates isolated engines and downloads pinned tools/models outside this source folder, under `%LOCALAPPDATA%\NguyenHuuThinhWorkflow\runtime-1.1.0`. It never copies a venv from another machine or changes the system PATH. This is an online setup package, not an offline portable runtime. Keep the generated `config/local.json` private to the installed machine.

```powershell
.\workflow.ps1 doctor --online
```

Only `READY_ENGINE_SMOKE_PASS` means real GPU inference and the online Ava check passed. An offline import check is insufficient. CapCut recognition and all film editorial reviews remain explicit manual steps.

## Start a different film

Replace `SERIES_ID` and `N` with the selected film's numeric ID and its full catalog episode count. Each run directory must be new; always use `1-N`:

```powershell
.\workflow.ps1 init --series-id SERIES_ID --episodes 1-N --run '.\runs\new-film-v001'
.\workflow.ps1 preflight --run '.\runs\new-film-v001'
.\workflow.ps1 disk --run '.\runs\new-film-v001'
.\workflow.ps1 download --run '.\runs\new-film-v001'
.\workflow.ps1 prepare --run '.\runs\new-film-v001'
```

Open the reported fresh draft in CapCut. Run **Auto captions → Chinese**, then save. Draft creation does not perform recognition. Do not trim, reorder or change source speed.

```powershell
.\workflow.ps1 extract --run '.\runs\new-film-v001'
.\workflow.ps1 editorial-export --run '.\runs\new-film-v001'
```

Continue with [the editorial and production runbook](docs/WORKFLOW.md). Every required input is produced as a blank draft by the CLI. Human/Codex editorial work fills these drafts; scripts validate evidence and coverage but do not invent translations or approvals.

## Inspect or continue

```powershell
.\workflow.ps1 status --run '.\runs\new-film-v001'
.\workflow.ps1 resume --run '.\runs\new-film-v001'
```

`resume` reports the checkpoint and next action. Run that action explicitly. It does not redownload or recreate a draft. Completed runs and sealed deliveries are immutable; create a versioned run for a new production.

The output is one MP4 and one English SRT with lineage, glossary, source/engine locks, measured QA and SHA256 inventory under `deliveries/`. The result remains **DRAFT** until actual independent listening and semantic review. Excluding the music/speech stems does not prove their complete absence from the SFX stem.

See [setup details](docs/SETUP.md), [release changes](CHANGELOG.md), `release.json` and `validation/acceptance.json` inside the verified ZIP. A software STABLE label applies to the tested workflow and profile, not to a film's unperformed reviews.
