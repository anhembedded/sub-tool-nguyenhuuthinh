"""Prepare hash-bound encoded name contacts; record explicit actual reviews."""
import argparse
import math
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from hongguo_lite.common import load, atomic, sha, utc, process_lock
from hongguo_lite.season_contracts import unsealed

CHECKS = ('chinese_covered', 'english_correct', 'no_clipping', 'max_two_lines', 'position_correct')


def _context(root):
    root = Path(root).resolve()
    unsealed(root)
    m = load(root / 'manifest.json')
    active = load(root / 'active-video-generation.json')
    run = Path(active['run'])
    if not (run / 'video-complete.json').exists():
        raise ValueError('Video build is incomplete')
    for path, expected in load(run / 'dependency-snapshot.json').items():
        if sha(path) != expected:
            raise ValueError('Video generation dependencies changed')
    names = {c['cue_id']: c for ep in m['episodes']
             for c in load(root / 'translation' / f'episode{ep:03d}.json')['cues'] if c['proper_names']}
    return root, m, active, run, names


def _items(run, names):
    result = {}
    for path in sorted(run.glob('ep*-name-frames.json')):
        report = load(path)
        if sha(report['video']) != report['video_sha256'] or sha(report['layout']) != report['layout_sha256']:
            raise ValueError('Encoded video or layout changed')
        for item in report['items']:
            cid = item['cue_id']
            if (cid in result or cid not in names or item['canonical_text'] != names[cid]['canonical_text']
                    or item['episode'] != names[cid]['episode'] or sha(item['image']) != item['image_sha256']):
                raise ValueError('Encoded frame/canonical selection differs')
            result[cid] = dict(item, video_sha256=report['video_sha256'],
                               report=str(path), report_sha256=sha(path))
    if set(result) != set(names):
        raise ValueError('Encoded name frames do not exactly cover canonical name cues')
    return result


def _validate_previous(previous, current, generation):
    if previous.get('video_generation') != generation:
        raise ValueError('Previous visual review belongs to another video generation')
    result = {}
    for item in previous.get('items', []):
        cid = item['cue_id']
        if cid in result or cid not in current:
            raise ValueError('Unexpected or duplicated reviewed cue')
        live = current[cid]
        if any(item.get(k) != live[k] for k in ('episode', 'canonical_text', 'frame', 'image_sha256', 'video_sha256')):
            raise ValueError('Prior reviewed frame/video changed')
        if (item.get('status') != 'PASS' or set(item.get('checks', {})) != set(CHECKS)
                or any(item['checks'][k] is not True for k in CHECKS)
                or not item.get('reviewer') or not item.get('note')):
            raise ValueError('Prior visual review lacks actual approval checks')
        if sha(item['image']) != item['image_sha256'] or sha(item['contact']) != item['contact_sha256']:
            raise ValueError('Prior visual review evidence changed')
        result[cid] = item
    return result


def prepare(root):
    root, m, active, run, names = _context(root)
    current = _items(run, names)
    review_path = run / 'name-visual-review.json'
    if not names:
        atomic(review_path, dict(status='NOT_REQUIRED', video_generation=active['identity'],
               reviewed_count=0, expected_count=0, items=[],
               reason='No proper-name cues; no visual name review was performed.'))
        print('Encoded name review NOT_REQUIRED: zero selected cues', flush=True)
        return dict(status='NOT_REQUIRED', review=str(review_path))
    previous = _validate_previous(load(review_path), current, active['identity']) if review_path.exists() else {}
    items = [item for cid, item in current.items() if cid not in previous]
    items.sort(key=lambda c: (c['episode'], c['frame'], c['cue_id']))
    batch = run / 'review-batches' / f'batch-{time.time_ns()}'
    batch.mkdir(parents=True)
    geometry = load(root / 'geometry.json')
    font_path = m['environment'].get('preview_font') or m['environment'].get('font')
    font = ImageFont.truetype(str(font_path), 16) if font_path else ImageFont.load_default()
    contacts = []
    for start in range(0, len(items), 10):
        group = items[start:start + 10]
        sheet = Image.new('RGB', (1920, 300 * math.ceil(len(group) / 2)), '#222222')
        draw = ImageDraw.Draw(sheet)
        for index, item in enumerate(group):
            x, y = index % 2 * 960, index // 2 * 300
            with Image.open(item['image']) as im:
                im = im.convert('RGB')
                thumb = im.copy()
                thumb.thumbnail((320, 180))
                sheet.paste(thumb, (x, y + 75))
                box = geometry[str(item['episode'])]['blur_box']
                band = im.crop((0, max(0, box['y'] - 60), im.width,
                                min(im.height, box['y'] + box['height'] + 60)))
                band.thumbnail((630, 190))
                sheet.paste(band, (x + 325, y + 75))
            draw.text((x + 5, y + 3), f"{item['cue_id']} EP{item['episode']:03d} frame {item['frame']}", font=font, fill='white')
            text = item['canonical_text']
            for line, chunk in enumerate([text[i:i + 100] for i in range(0, len(text), 100)]):
                draw.text((x + 5, y + 23 + line * 18), chunk, font=font, fill='#ffdd00')
        contact = batch / f'contact-{start // 10:03d}.png'
        sheet.save(contact)
        contacts.append(dict(path=str(contact), sha256=sha(contact), items=group))
    atomic(batch / 'batch.json', dict(video_generation=active['identity'], contacts=contacts,
           status='AWAITING_ACTUAL_VISUAL_REVIEW'))
    print(str(batch), flush=True)
    print('Unseen encoded name frames:', len(items), flush=True)
    return dict(status='AWAITING_ACTUAL_VISUAL_REVIEW', batch=str(batch), unseen=len(items))


def record(root, batch, indices, note, reviewer=None):
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(note, str) or not note.strip():
        raise ValueError('Actual reviewer identity and an explicit visual-review note are required')
    root, m, active, run, names = _context(root)
    if not names:
        raise ValueError('No name cues; review-prepare records NOT_REQUIRED without a fabricated review')
    current = _items(run, names)
    doc = load(Path(batch) / 'batch.json')
    if doc['video_generation'] != active['identity']:
        raise ValueError('Wrong video generation')
    if (not indices or len(set(indices)) != len(indices)
            or any(type(i) is not int or not 0 <= i < len(doc['contacts']) for i in indices)):
        raise ValueError('Explicit unique nonnegative contact indices are required')
    target = run / 'name-visual-review.json'
    items = _validate_previous(load(target), current, active['identity']) if target.exists() else {}
    for index in indices:
        contact = doc['contacts'][index]
        if sha(contact['path']) != contact['sha256']:
            raise ValueError('Viewed contact changed')
        for item in contact['items']:
            cid = item['cue_id']
            if cid not in current or item != current[cid] or item['layout_errors']:
                raise ValueError('Video/frame/layout binding failed')
            approved = dict(item, status='PASS', checks={k: True for k in CHECKS},
                            reviewer=reviewer.strip(), reviewed_utc=utc(), contact=contact['path'],
                            contact_sha256=contact['sha256'], note=note.strip())
            items[cid] = approved
    atomic(target, dict(status='PASS' if set(items) == set(names) else 'PARTIAL',
           video_generation=active['identity'], reviewed_count=len(items), expected_count=len(names),
           items=sorted(items.values(), key=lambda i: i['cue_id'])))
    print(f'Recorded {len(items)}/{len(names)} explicitly reviewed name frames', flush=True)
    return load(target)


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('action', choices=['prepare', 'record'])
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--batch', type=Path)
    p.add_argument('--contacts', type=int, nargs='+')
    p.add_argument('--note')
    p.add_argument('--reviewer')
    a = p.parse_args()
    with process_lock(a.run / 'visual-review.lock'):
        if a.action == 'prepare':
            prepare(a.run)
        else:
            if not a.batch or not a.contacts or not a.note or not a.reviewer:
                p.error('Explicit viewed contacts, note and reviewer are required')
            record(a.run, a.batch, a.contacts, a.note, a.reviewer)


if __name__ == '__main__':
    main()
