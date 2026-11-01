# Run a fresh film

Use the setup and source preparation commands in the root README first. Run commands from this extracted distribution, replacing `$run` with the new run directory. The only supported media profile is whole-series Hongguo 1920×1080 HEVC CFR30 SDR. Every file and decision below belongs to that run.

```powershell
$run = '.\runs\new-film-v001'
```

## Contextual English

After `extract`, inspect the reported `extraction-report.json`. Boundary crossings, overlaps, missing captions or a changed CapCut timeline require source review before production; no stage silently clips or relabels them. Keep the raw saved draft/SRT unchanged.

Run `editorial-export`. Edit the printed `editorial-draft.json`: set the actual `reviewer`, hash-bound `evidence`, and make a KEEP/EXCLUDE decision for every source cue. For kept dialogue, fill contextual single-line English `canonical_text`, classification and explicit `proper_names` (use `[]` for no names). Exclusions need their reason and evidence. Do not edit raw text, IDs or times. Create a fresh glossary mapping each marked English name to `source_variants` and optional `english_aliases`.

Evidence is an array of objects such as `{"path":"absolute/or/relative/file","sha256":"actual SHA256"}`; relative paths resolve beside the edited input. Obtain a hash with `Get-FileHash -Algorithm SHA256`. Evidence must substantiate the editorial choice; a matching hash alone is not a semantic review.

```powershell
.\workflow.ps1 editorial-import --run $run --input "$run\editorial-draft.json"
.\workflow.ps1 geometry-prepare --run $run
```

## Measure caption geometry

Open the exported native samples for **every episode**, including episodes without proper names. Edit the printed geometry draft with the actual reviewer, each episode's `reviewed: true`, a note and its measured `blur_box` (`x`, `y`, `width`, `height`, even native pixels). Keep the bound evidence image paths/hashes. Check high, low and transition samples; request new evidence if the supplied samples do not establish a safe box. Never copy an earlier film's box.

To sample additional caption positions or transitions, create a JSON file such as `{"additional_frames":{"1":[15,42],"2":[23]}}` and run `geometry-prepare --run $run --input '.\geometry-extra.json'`. Indices are zero-based native frames within each episode. These frames are appended to the default first/middle/last/longest-cue samples; the request is hash-bound in the new sample report. Open the new images and edit the newly printed draft before import; extra sampling does not approve geometry.

```powershell
.\workflow.ps1 geometry-import --run $run --input 'PATH_PRINTED_BY_GEOMETRY_PREPARE'
.\workflow.ps1 ocr --run $run
```

## Resolve proper names

OCR selects only current proper-name cues and uses their native midpoint and measured caption region. Inspect the original images and raw OCR, then edit the reported `decisions-draft.json` with reviewer/evidence and one decision per selected cue:

- `MATCH_KEEP`: readable source agrees; preserve English.
- `UNREADABLE_KEEP`: the source name cannot be read; preserve the previous English spelling.
- `MISMATCH_USE_OCR`: readable source differs; record `observed_name`, corrected English, names, glossary and reason.

Each decision binds the actual source image hash. The program never converts an OCR confidence score into approval. For a film with zero marked names, OCR emits `NOT_REQUIRED` without loading GPU models; still run `names-import` to finalize the canonical snapshot.

```powershell
.\workflow.ps1 names-import --run $run --input 'PATH_PRINTED_BY_OCR'
```

## Speech, SFX, video and visual review

```powershell
.\workflow.ps1 speech --run $run
.\workflow.ps1 background --run $run
.\workflow.ps1 video --run $run
.\workflow.ps1 review-prepare --run $run
```

Ava runs at `+0%`; measured fit is 1.25–5×, without cutting active audio or slowing video. A cue that does not fit requires editorial correction and a recorded revision. MVSEP runs on NVIDIA GPU FP32; only the `sfx` stem is mixed at −5 dB, while all source stem evidence is retained. GPU stages share a machine-wide lock.

Open every reported encoded contact sheet for names. Record only contacts actually viewed, with your actual reviewer identity and a meaningful note. The indices are zero-based:

```powershell
.\workflow.ps1 review-record --run $run --batch 'REPORTED_BATCH_FOLDER' --contacts 0 1 --reviewer 'Actual reviewer' --note 'Observed checks and any relevant details'
.\workflow.ps1 pts --run $run
.\workflow.ps1 finish --run $run
.\workflow.ps1 package --run $run
.\workflow.ps1 seal --run $run
```

No-name films receive explicit `NOT_REQUIRED` for the name-only review; this does not approve all visual content. `package` requires matching canonical, speech, source timing and visual evidence. It verifies the final MP4 and SRT before publishing the delivery directory. `seal` hashes the complete delivered tree. Do not add files afterward.

## Errors and revisions

On an interrupted fit or a Store-backed media stage, rerun the same explicit action; failed attempts remain intact and verified work can be reused. A draft left by an interrupted `prepare` is preserved rather than overwritten: inspect its saved state and use a new versioned run if preparation did not commit its receipt. Service rejection is reported, with no alternate voice, source, OCR model or CPU fallback. Disk estimates include retained attempts and evidence; free additional space instead of deleting history automatically.

For canonical edits after a video generation, save the previous active video pointer and its dependency snapshot first. Reimport the updated editorial draft, rerun OCR/name decisions as applicable and measure the new speech. Create `canonical-revision.json` with `base_video` (the saved previous pointer), `episodes` listing affected episodes and `changed_files` mapping every changed dependency path to its previous snapshot `before_sha256` and current `after_sha256`. Run `revision`; the runner archives this ledger and resolves inherited episode stores across multiple revisions. Recheck the changed encoded contacts, then `pts`, `finish` and `package` for an unsealed run. A completed/sealed production requires a new run version.

Output remains DRAFT with independent listening/semantics `NOT_PERFORMED` until those reviews actually occur. Name review, full decode and SFX stem selection are distinct evidence and cannot substitute for listening or translation review.
