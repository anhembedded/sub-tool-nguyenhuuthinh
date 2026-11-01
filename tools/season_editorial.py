"""Fresh-film editorial handoffs. Approval always comes from an explicit import.

The draft JSON files are editing interfaces, not acceptance receipts. Raw CapCut
exports and source cue fields are immutable; imports account for every raw cue.
"""
import argparse
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from hongguo_lite.common import atomic, digest, load, process_lock, sha, utc
from hongguo_lite.season_contracts import unsealed

LINEAGE = ('cue_id', 'source_cue_id', 'episode', 'source_text',
           'source_start_us', 'source_end_us', 'start_frame', 'end_frame_exclusive')
CLASSIFICATIONS = {'DIALOGUE', 'MIXED', 'NONVERBAL', 'ANOMALY'}


def _path(value, base):
    p = Path(value)
    return (p if p.is_absolute() else Path(base) / p).resolve()


def _reviewer(doc, reviewer=None):
    value = reviewer or doc.get('reviewer', '')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('An explicit editorial reviewer is required')
    if reviewer and doc.get('reviewer') and reviewer != doc['reviewer']:
        raise ValueError('Reviewer differs from the submitted document')
    return value.strip()


def _evidence(items, base, *, required=True):
    if not isinstance(items, list) or (required and not items):
        raise ValueError('Explicit hash-bound evidence is required')
    result = []
    for item in items:
        if not isinstance(item, dict) or not item.get('path') or not item.get('sha256'):
            raise ValueError('Evidence needs path and sha256')
        p = _path(item['path'], base)
        if not p.is_file() or sha(p) != item['sha256']:
            raise ValueError(f'Evidence changed: {p}')
        result.append({**item, 'path': str(p)})
    return result


def _sources(root):
    unsealed(root)
    m = load(root / 'manifest.json')
    d = load(root / 'download/download_report.json')
    rows = d.get('episodes', [])
    if (d.get('status') != 'complete' or d.get('seriesId') != m['series_id']
            or [r['episode'] for r in rows] != m['episodes']):
        raise ValueError('Verified source report must match the complete manifest scope')
    for row in rows:
        v = row['validation']
        if (row.get('status') != 'ready' or v['fullDecodeExitCode'] != 0
                or sha(row['path']) != v['sha256'] or Fraction(v['frameRate']) != 30
                or type(v['frameCount']) is not int or v['frameCount'] <= 0):
            raise ValueError(f"Unverified native source EP{row['episode']}")
    return m, rows


def _source_description(row):
    stream = next(s for s in row['validation']['probe']['streams'] if s['codec_type'] == 'video')
    return dict(episode=row['episode'], sha256=row['validation']['sha256'],
                frames=row['validation']['frameCount'], fps='30',
                width=stream['width'], height=stream['height'])


def _translations(root):
    m = load(root / 'manifest.json')
    docs = {ep: load(root / 'translation' / f'episode{ep:03d}.json') for ep in m['episodes']}
    cues = [c for ep in m['episodes'] for c in docs[ep]['cues']]
    if len({c['cue_id'] for c in cues}) != len(cues):
        raise ValueError('Duplicate canonical cue identity')
    return docs, cues


def _archive(root, names):
    paths = [root / n for n in names if (root / n).exists()]
    if not paths:
        return None
    out = root / 'editorial-history' / f'import-{time.time_ns()}'
    out.mkdir(parents=True)
    for p in paths:
        target = out / p.relative_to(root)
        if p.is_dir():
            shutil.copytree(p, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    return str(out)


def _text(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(c in value for c in '\r\n\t') or re.search('[\u3400-\u9fff]', value)):
        raise ValueError('Canonical English must be nonempty, single-line contextual text')
    return value


def _proper_names(value, text):
    if (not isinstance(value, list) or any(not isinstance(n, str) or not n.strip()
            or n != n.strip() or n not in text for n in value)
            or len(set(value)) != len(value)):
        raise ValueError('proper_names must explicitly list unique names appearing in canonical text')
    return value


def _snapshot(root, scope):
    return {str(root / 'translation' / f'episode{ep:03d}.json'):
            sha(root / 'translation' / f'episode{ep:03d}.json') for ep in scope}


def _glossary(glossary, cues):
    if not isinstance(glossary, dict):
        raise ValueError('A fresh-film glossary object is required; use {} when there are no names')
    aliases = set()
    for name, entry in glossary.items():
        if (not isinstance(name, str) or not name.strip() or not isinstance(entry, dict)
                or not isinstance(entry.get('source_variants'), list)
                or not entry['source_variants'] or any(not isinstance(n, str) or not n.strip() for n in entry['source_variants'])
                or not isinstance(entry.get('english_aliases', []), list)
                or any(not isinstance(n, str) or not n.strip() for n in entry.get('english_aliases', []))):
            raise ValueError('Glossary entries need source_variants and optional english_aliases')
        aliases.add(name)
        aliases.update(entry.get('english_aliases', []))
    if any(n not in aliases for c in cues for n in c['proper_names']):
        raise ValueError('Every marked proper name needs a fresh-film glossary entry')
    return glossary


def editorial_export(root):
    root = Path(root).resolve()
    m, rows = _sources(root)
    if (root / 'input.json').exists() or (root / 'editorial-draft.json').exists():
        raise FileExistsError('Editorial export already exists; edit its draft or use a new run')
    export = _path(m['latest_export'], root)
    if not export.is_relative_to(root):
        raise ValueError('Use this fresh run\'s own saved extraction')
    report = load(export / 'extraction-report.json')
    sources = {str(r['episode']): r['validation']['sha256'] for r in rows}
    if (report.get('source_report_sha256') != sha(root / 'download/download_report.json')
            or report.get('sources') != sources):
        raise ValueError('Saved extraction is not bound to this fresh film source ledger')
    for name, expected in report['files'].items():
        if Path(name).name != name or sha(export / name) != expected:
            raise ValueError('Saved extraction snapshot changed')
    if (sha(export / 'raw.zh.srt') != report['raw_srt_sha256']
            or sha(export / 'saved-draft.json') != report['draft_sha256']):
        raise ValueError('Raw CapCut export changed')
    if report.get('timeline_anomalies') or any('CROSSING_OR_OUTSIDE_BOUNDARY' in a['reasons'] for a in report.get('anomalies', [])):
        raise ValueError('Resolve timeline/crossing anomalies in a new CapCut extraction before importing')
    saved = report['boundaries']
    if [b['episode'] for b in saved] != m['episodes']:
        raise ValueError('Extraction scope differs from manifest')
    offset = 0
    boundaries, cues = [], []
    for row, boundary in zip(rows, saved):
        ep, frames = row['episode'], row['validation']['frameCount']
        if (round(Fraction(boundary['start_us'] * 30, 1000000)) != offset
                or round(Fraction(boundary['end_us'] * 30, 1000000)) != offset + frames):
            raise ValueError('Saved draft boundaries differ from the exact native frame clock')
        boundaries.append(dict(episode=ep, start_seconds=offset / 30,
                               end_seconds=(offset + frames) / 30, frames=frames,
                               start_frame=offset, end_frame_exclusive=offset + frames,
                               saved_draft_start_us=boundary['start_us']))
        for raw in load(export / f'episode{ep:03d}.json')['cues']:
            source_id = raw['cue_id']
            if type(source_id) is not int or source_id <= 0:
                raise ValueError('Raw CapCut cue needs its original positive numeric ID')
            a, b = raw['start_us'], raw['end_us']
            if not 0 <= a < b <= boundary['end_us'] - boundary['start_us']:
                raise ValueError('Raw cue falls outside saved episode boundary')
            cues.append(dict(cue_id=f'cc-{source_id:05d}', source_cue_id=source_id,
                             episode=ep, source_text=raw['source_text'],
                             source_start_us=boundary['start_us'] + a,
                             source_end_us=boundary['start_us'] + b,
                             start_frame=offset + round(Fraction(a * 30, 1000000)),
                             end_frame_exclusive=offset + round(Fraction(b * 30, 1000000)),
                             canonical_text='', translation_status='PENDING_CONTEXTUAL_TRANSLATION',
                             proper_names=[], pronunciation_overrides=[]))
        offset += frames
    if len(cues) != report['cue_count'] or len({c['cue_id'] for c in cues}) != len(cues):
        raise ValueError('Every raw CapCut cue must occur exactly once in the export')
    clock = dict(status='NATIVE_FRAME_EQUIVALENT_NO_RETIME', created_utc=utc(),
                 raw_export=str(export), raw_draft_sha256=report['draft_sha256'],
                 source_frame_total=offset, boundaries=boundaries, cue_changes=[],
                 note='Round saved source-local microseconds to native CFR30 frames; no clipping, shift or retime.')
    atomic(root / 'native-clock-conform.json', clock)
    plan = dict(source_srt=str(export / 'raw.zh.srt'), source_srt_sha256=report['raw_srt_sha256'],
                cues=cues, voice='en-US-AvaMultilingualNeural', edge_rate='+0%',
                fit_policy='pyvideotrans-audio-only-ratio-v1', min_tempo=1.25, max_tempo=5,
                fps='30', total_frames=offset, episode_boundaries=boundaries,
                source_report_sha256=report['source_report_sha256'], source_hashes=sources,
                extraction_report_sha256=sha(export / 'extraction-report.json'),
                native_clock_conform_sha256=sha(root / 'native-clock-conform.json'))
    atomic(root / 'input.json', plan)
    for ep in m['episodes']:
        atomic(root / 'translation' / f'episode{ep:03d}.json',
               dict(episode=ep, cues=[c for c in cues if c['episode'] == ep]))
    draft = dict(schema=1, series_id=m['series_id'], input_sha256=sha(root / 'input.json'),
                 reviewer='', evidence=[], glossary={}, source_corrections=[],
                 independent_semantics='NOT_PERFORMED', independent_listening='NOT_PERFORMED',
                 cues=[dict(**{k: c[k] for k in LINEAGE}, action='PENDING',
                            classification='PENDING', reason='', evidence=[],
                            canonical_text='', proper_names=[]) for c in cues])
    atomic(root / 'editorial-draft.json', draft)
    return dict(status='AWAITING_CONTEXTUAL_EDITORIAL_IMPORT', draft=str(root / 'editorial-draft.json'),
                episodes=len(m['episodes']), source_cues=len(cues))


def editorial_import(root, input_path, reviewer=None):
    root = Path(root).resolve()
    input_path = Path(input_path).resolve()
    doc = load(input_path)
    person = _reviewer(doc, reviewer)
    evidence = _evidence(doc.get('evidence'), input_path.parent)
    m, rows = _sources(root)
    plan = load(root / 'input.json')
    if (doc.get('series_id') != m['series_id'] or doc.get('input_sha256') != sha(root / 'input.json')
            or sha(plan['source_srt']) != plan['source_srt_sha256']
            or plan['source_report_sha256'] != sha(root / 'download/download_report.json')
            or plan['source_hashes'] != {str(r['episode']): r['validation']['sha256'] for r in rows}
            or sha(root / 'native-clock-conform.json') != plan['native_clock_conform_sha256']):
        raise ValueError('Stale editorial draft or changed raw lineage')
    if doc.get('independent_semantics') != 'NOT_PERFORMED' or doc.get('independent_listening') != 'NOT_PERFORMED':
        raise ValueError('Editorial import cannot certify independent listening or semantic review')
    incoming = doc.get('cues', [])
    if [c.get('cue_id') for c in incoming] != [c['cue_id'] for c in plan['cues']]:
        raise ValueError('Every raw cue must be accounted for exactly once, in source order')
    kept, dispositions = [], []
    for original, item in zip(plan['cues'], incoming):
        if any(item.get(k) != original[k] for k in LINEAGE):
            raise ValueError('Raw source text, identity and timing are immutable')
        action, classification = item.get('action'), item.get('classification')
        if action not in ('KEEP', 'EXCLUDE') or classification not in CLASSIFICATIONS:
            raise ValueError('Every cue requires explicit KEEP/EXCLUDE and classification')
        proof = _evidence(item.get('evidence', []), input_path.parent, required=action == 'EXCLUDE')
        if action == 'EXCLUDE' and (not isinstance(item.get('reason'), str) or not item['reason'].strip()):
            raise ValueError('Exclusion requires a specific editorial reason')
        dispositions.append(dict(cue_id=original['cue_id'], source_cue_id=original['source_cue_id'],
                                 episode=original['episode'], source_text=original['source_text'],
                                 classification=classification, decision=action,
                                 reason=item.get('reason', ''), evidence=proof))
        if action == 'KEEP':
            text = _text(item.get('canonical_text'))
            names = _proper_names(item.get('proper_names'), text)
            kept.append({**original, 'canonical_text': text, 'proper_names': names,
                         'translation_status': 'DRAFT_PENDING_NAME_OCR' if names else 'CONTEXTUAL_CANONICAL',
                         'editorial_status': classification,
                         'translation_evidence': dict(reviewer=person, evidence=proof or evidence)})
    if not kept:
        raise ValueError('At least one canonical spoken cue is required for this workflow')
    from hongguo_lite.media import _validate_cues
    _validate_cues(kept, dict(frames=plan['total_frames']))
    glossary = _glossary(doc.get('glossary'), kept)
    originals = {c['cue_id']: c for c in plan['cues']}
    corrections, seen = [], set()
    source_hashes = {r['episode']: r['validation']['sha256'] for r in rows}
    for item in doc.get('source_corrections', []):
        cid = item.get('cue_id')
        if cid not in originals or cid in seen or not item.get('observed_source_caption') or not item.get('reason'):
            raise ValueError('Source corrections need a unique original cue, observation and reason')
        proof = _evidence(item.get('evidence'), input_path.parent)
        ep = originals[cid]['episode']
        if any(p.get('episode') != ep or p.get('source_sha256') != source_hashes[ep] for p in proof):
            raise ValueError('Source correction evidence must identify its episode and source hash')
        corrections.append({**item, 'raw_source_text': originals[cid]['source_text'],
                            'evidence': proof, 'raw_source_unchanged': True})
        seen.add(cid)
    history = _archive(root, ['translation', 'name-decisions.json', 'canonical-summary.json',
                             'dialogue-dispositions.json', 'glossary.json',
                             'source-corrections.json', 'proper-name-selection.json'])
    for ep in m['episodes']:
        atomic(root / 'translation' / f'episode{ep:03d}.json',
               dict(episode=ep, cues=[c for c in kept if c['episode'] == ep]))
    selected = [c for c in kept if c['proper_names']]
    atomic(root / 'dialogue-dispositions.json', dict(status='EDITORIAL_DISPOSITION_COMPLETE',
           reviewer=person, created_utc=utc(), evidence=evidence, items=dispositions,
           source_cues=len(incoming), kept=len(kept), omitted=len(incoming) - len(kept)))
    atomic(root / 'glossary.json', glossary)
    atomic(root / 'source-corrections.json', dict(status='EDITORIAL_SOURCE_FRAME_REVIEW',
           reviewer=person, created_utc=utc(), items=corrections, evidence=evidence))
    atomic(root / 'proper-name-selection.json', dict(policy='Explicit editorial proper_names only; native midpoint per cue.', cues=selected))
    atomic(root / 'canonical-summary.json', dict(status='AWAITING_NAME_DECISIONS',
           source_cues=len(incoming), cues=len(kept), omitted=len(incoming) - len(kept),
           episodes=len(m['episodes']), name_cues=len(selected), independent_semantic_approval=False))
    atomic(root / 'name-decisions.json', dict(status='AWAITING_NAME_DECISIONS',
           history=history, canonical_snapshot=_snapshot(root, m['episodes']), decisions=[]))
    return dict(status='AWAITING_NAME_DECISIONS', cues=len(kept), name_cues=len(selected))


def geometry_prepare(root, input_path=None):
    root = Path(root).resolve()
    m, rows = _sources(root)
    plan = load(root / 'input.json')
    requested = {}
    request_sha256 = None
    if input_path is not None:
        input_path = Path(input_path).resolve()
        request = load(input_path)
        if not isinstance(request, dict) or set(request) != {'additional_frames'} or not isinstance(request['additional_frames'], dict):
            raise ValueError('Geometry sampling input must contain an additional_frames object')
        requested = request['additional_frames']
        if not set(requested) <= {str(ep) for ep in m['episodes']}:
            raise ValueError('Additional geometry frames reference an episode outside the manifest scope')
        lengths = {str(r['episode']): r['validation']['frameCount'] for r in rows}
        for ep, frames in requested.items():
            if (not isinstance(frames, list) or any(type(frame) is not int or not 0 <= frame < lengths[ep] for frame in frames)
                    or len(set(frames)) != len(frames)):
                raise ValueError('Additional geometry frames must be unique native integer indices within the episode')
        request_sha256 = sha(input_path)
    identity = digest(dict(input=sha(root / 'input.json'), sources={r['episode']: r['validation']['sha256'] for r in rows},
                           ffmpeg=sha(m['environment']['ffmpeg']), worker=sha(__file__),
                           additional_frames=requested, selection_input_sha256=request_sha256))
    out = root / 'geometry-evidence' / f'{identity[:16]}-{time.time_ns()}'
    out.mkdir(parents=True)
    selection_snapshot = None
    if input_path is not None:
        selection_snapshot = out / 'sampling-request.json'
        shutil.copy2(input_path, selection_snapshot)
        if sha(selection_snapshot) != request_sha256:
            raise ValueError('Geometry sampling request changed during preparation')
    samples, entries = [], {}
    from PIL import Image, ImageDraw
    for row in rows:
        ep = row['episode']
        src = _source_description(row)
        boundary = next(b for b in plan['episode_boundaries'] if b['episode'] == ep)
        offset = round(boundary['start_seconds'] * 30)
        cues = [c for c in plan['cues'] if c['episode'] == ep]
        if cues:
            choices = [cues[0], cues[len(cues) // 2], cues[-1], max(cues, key=lambda c: len(c['source_text']))]
            frames = sorted({min(src['frames'] - 1, max(0, (c['start_frame'] + c['end_frame_exclusive']) // 2 - offset)) for c in choices})
        else:
            frames = sorted({0, src['frames'] // 2, src['frames'] - 1})
        frames = sorted(set(frames) | set(requested.get(str(ep), [])))
        folder = out / f'ep{ep:03d}'
        folder.mkdir()
        expr = '+'.join(f'eq(n\\,{n})' for n in frames)
        subprocess.run([m['environment']['ffmpeg'], '-v', 'error', '-nostdin', '-n', '-threads', '2',
                        '-i', row['path'], '-vf', f'select={expr}', '-fps_mode', 'vfr',
                        '-frames:v', str(len(frames)), str(folder / 'frame-%04d.png')], check=True)
        images = sorted(folder.glob('frame-*.png'))
        if len(images) != len(frames):
            raise ValueError('Geometry sample extraction incomplete')
        proof = []
        contact = Image.new('RGB', (960, 300 * len(images)), '#222222')
        draw = ImageDraw.Draw(contact)
        for index, (frame, image) in enumerate(zip(frames, images)):
            with Image.open(image) as im:
                if im.size != (src['width'], src['height']):
                    raise ValueError('Geometry sample dimensions differ from native source')
                thumb = im.convert('RGB')
                thumb.thumbnail((960, 270))
                contact.paste(thumb, ((960 - thumb.width) // 2, index * 300 + 25))
            draw.text((8, index * 300 + 5), f'EP{ep:03d} native frame {frame}', fill='white')
            proof.append(dict(path=str(image), sha256=sha(image), episode=ep,
                              frame=frame, source_sha256=src['sha256']))
        contact_path = folder / 'contact.png'
        contact.save(contact_path)
        samples.append(dict(episode=ep, source_sha256=src['sha256'], width=src['width'], height=src['height'],
                            evidence=proof, contact=str(contact_path), contact_sha256=sha(contact_path)))
        entries[str(ep)] = dict(verified=False, reviewed=False, source_sha256=src['sha256'], evidence=proof,
                               blur_box=dict(x=None, y=None, width=None, height=None), note='')
    report = out / 'samples.json'
    atomic(report, dict(identity=identity, input_sha256=sha(root / 'input.json'), samples=samples,
           additional_frames=requested, selection_input_sha256=request_sha256,
           selection_snapshot=str(selection_snapshot) if selection_snapshot else None))
    draft = out / 'geometry-draft.json'
    atomic(draft, dict(schema=1, reviewer='', sample_report=str(report), sample_report_sha256=sha(report), episodes=entries))
    return dict(status='AWAITING_ACTUAL_GEOMETRY_REVIEW', draft=str(draft), report=str(report))


def geometry_import(root, input_path, reviewer=None):
    root = Path(root).resolve()
    input_path = Path(input_path).resolve()
    doc = load(input_path)
    person = _reviewer(doc, reviewer)
    report_path = _path(doc['sample_report'], input_path.parent)
    if sha(report_path) != doc['sample_report_sha256']:
        raise ValueError('Geometry sample report changed')
    report = load(report_path)
    if report.get('selection_snapshot') and sha(report['selection_snapshot']) != report['selection_input_sha256']:
        raise ValueError('Geometry sampling request snapshot changed')
    if report['input_sha256'] != sha(root / 'input.json'):
        raise ValueError('Geometry samples belong to a different input')
    m, rows = _sources(root)
    if set(doc.get('episodes', {})) != {str(ep) for ep in m['episodes']}:
        raise ValueError('Geometry must cover exactly the manifest episodes')
    from hongguo_lite.media import geometry_box
    result = {}
    for row in rows:
        ep = row['episode']
        entry = doc['episodes'][str(ep)]
        sample = next(s for s in report['samples'] if s['episode'] == ep)
        src = _source_description(row)
        if entry.get('reviewed') is not True or not isinstance(entry.get('note'), str) or not entry['note'].strip():
            raise ValueError('Each episode needs an explicit actual-review confirmation and note')
        if entry.get('source_sha256') != src['sha256'] or sample['source_sha256'] != src['sha256']:
            raise ValueError('Geometry source binding changed')
        proof = _evidence(entry.get('evidence'), input_path.parent)
        allowed = {(p['path'], p['sha256'], p['frame']) for p in sample['evidence']}
        if any((p['path'], p['sha256'], p.get('frame')) not in allowed
               or p.get('episode') != ep or p.get('source_sha256') != src['sha256'] for p in proof):
            raise ValueError('Geometry evidence must be an extracted native sample from this episode')
        verified = {**entry, 'verified': True, 'reviewer': person, 'reviewed_utc': utc(), 'evidence': proof}
        box = verified.get('blur_box', {})
        if set(box) != {'x', 'y', 'width', 'height'} or any(type(v) is not int for v in box.values()):
            raise ValueError('Supply an explicit integer blur_box')
        geometry_box(src, verified)
        result[str(ep)] = verified
    _archive(root, ['geometry.json'])
    atomic(root / 'geometry.json', result)
    return dict(status='GEOMETRY_IMPORTED', episodes=len(result))


def names_import(root, input_path=None, reviewer=None):
    root = Path(root).resolve()
    unsealed(root)
    m = load(root / 'manifest.json')
    docs, cues = _translations(root)
    summary = load(root / 'canonical-summary.json')
    if summary.get('status') not in ('AWAITING_NAME_DECISIONS', 'CANONICAL_READY_FOR_TTS'):
        raise ValueError('Import the reviewed editorial document before name decisions')
    pending = load(root / 'name-decisions.json')
    if pending.get('canonical_snapshot') != _snapshot(root, m['episodes']):
        raise ValueError('Canonical changed after editorial import; reconcile it through editorial-import')
    for cue in cues:
        _text(cue.get('canonical_text'))
        _proper_names(cue.get('proper_names'), cue['canonical_text'])
        if cue.get('translation_status') not in ('DRAFT_PENDING_NAME_OCR', 'CONTEXTUAL_CANONICAL'):
            raise ValueError('Unresolved contextual translation before name decisions')
    selected = load(root / 'proper-name-selection.json')['cues']
    expected = {c['cue_id']: c for c in cues if c['proper_names']}
    if len(selected) != len(expected) or {c['cue_id'] for c in selected} != set(expected):
        raise ValueError('Name selection does not match current canonical names')
    if not expected:
        _archive(root, ['name-decisions.json'])
        result = dict(status='NOT_REQUIRED', reason='No editorial proper-name cues selected; no name review performed.',
                      canonical_snapshot=_snapshot(root, m['episodes']), decisions=[], independent_semantic_approval=False)
        atomic(root / 'name-decisions.json', result)
    else:
        if input_path is None:
            raise ValueError('Names import requires an explicitly reviewed decision document')
        input_path = Path(input_path).resolve()
        doc = load(input_path)
        person = _reviewer(doc, reviewer)
        evidence = _evidence(doc.get('evidence'), input_path.parent)
        active = load(root / 'active-name-ocr.json')
        report_path = Path(active['report'])
        if sha(report_path) != active['report_sha256'] or doc.get('ocr_report_sha256') != active['report_sha256']:
            raise ValueError('OCR report changed or decision document is stale')
        report = load(report_path)
        if report['status'] != 'AWAITING_EDITORIAL_NAME_REVIEW':
            raise ValueError('Name OCR is not ready for explicit review')
        jobs = {j['cue_id']: j for j in report['items']}
        if set(jobs) != set(expected):
            raise ValueError('OCR name coverage differs from current canonical selection')
        for path, expected_hash in report['files'].items():
            if sha(path) != expected_hash:
                raise ValueError('OCR evidence snapshot changed')
        decisions = doc.get('decisions', [])
        if len(decisions) != len(expected) or {d.get('cue_id') for d in decisions} != set(expected):
            raise ValueError('Each selected name cue needs exactly one decision')
        applied = []
        for item in decisions:
            cid = item['cue_id']
            cue, job = expected[cid], jobs[cid]
            if (job['canonical_text'] != cue['canonical_text'] or job['episode'] != cue['episode']
                    or job['source_text'] != cue['source_text'] or sha(job['image']) != job['image_sha256']):
                raise ValueError('OCR source/canonical binding changed; regenerate OCR')
            decision = item.get('decision')
            if decision not in ('MATCH_KEEP', 'UNREADABLE_KEEP', 'MISMATCH_USE_OCR'):
                raise ValueError('Explicit MATCH_KEEP, UNREADABLE_KEEP or MISMATCH_USE_OCR decision required')
            if not isinstance(item.get('reason'), str) or not item['reason'].strip():
                raise ValueError('Name decision needs an editorial reason')
            if type(item.get('readable')) is not bool or item['readable'] != (decision != 'UNREADABLE_KEEP'):
                raise ValueError('Name readability must explicitly agree with the decision')
            proof = _evidence(item.get('evidence'), input_path.parent)
            if not any(p['path'] == str(Path(job['image']).resolve()) and p['sha256'] == job['image_sha256'] for p in proof):
                raise ValueError('Name decision must bind the actual native cue image')
            text = _text(item.get('canonical_text'))
            names = _proper_names(item.get('proper_names'), text)
            if not names:
                raise ValueError('Name decisions cannot silently remove proper-name selection')
            if decision != 'MISMATCH_USE_OCR' and (text != cue['canonical_text'] or names != cue['proper_names']):
                raise ValueError('Keep decisions must preserve the current English text and names')
            if decision == 'MISMATCH_USE_OCR' and not item.get('observed_name'):
                raise ValueError('Readable mismatch requires an explicitly observed source name')
            applied.append({**item, 'episode': cue['episode'], 'before': cue['canonical_text'],
                            'after': text, 'evidence': proof, 'source_image': job['image'],
                            'source_image_sha256': job['image_sha256']})
            cue.update(canonical_text=text, proper_names=names)
        glossary = _glossary(doc.get('glossary', load(root / 'glossary.json')), cues)
        history = _archive(root, ['translation', 'name-decisions.json', 'canonical-summary.json', 'glossary.json'])
        for ep, episode in docs.items():
            for cue in episode['cues']:
                cue['translation_status'] = 'CONTEXTUAL_CANONICAL'
            atomic(root / 'translation' / f'episode{ep:03d}.json', episode)
        atomic(root / 'glossary.json', glossary)
        result = dict(status='APPLIED_BEFORE_TTS', reviewer=person, reviewed_utc=utc(), evidence=evidence,
                      ocr_report=str(report_path), ocr_report_sha256=active['report_sha256'],
                      canonical_snapshot=_snapshot(root, m['episodes']), decisions=applied,
                      history=history, independent_semantic_approval=False)
        atomic(root / 'name-decisions.json', result)
    summary['status'] = 'CANONICAL_READY_FOR_TTS'
    atomic(root / 'canonical-summary.json', summary)
    return dict(status=result['status'], name_cues=len(expected))


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('action', choices=['editorial-export', 'editorial-import', 'geometry-prepare', 'geometry-import', 'names-import'])
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--input', type=Path)
    p.add_argument('--reviewer')
    a = p.parse_args()
    if a.action in ('editorial-import', 'geometry-import') and not a.input:
        p.error('--input is required for this import')
    function = globals()[a.action.replace('-', '_')]
    with process_lock(a.run / 'editorial.lock'):
        if a.action.endswith('import'):
            result = function(a.run, a.input, a.reviewer)
        elif a.action == 'geometry-prepare':
            result = function(a.run, a.input)
        else:
            result = function(a.run)
    import json
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
