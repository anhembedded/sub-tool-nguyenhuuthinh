"""Shared, measured contracts for the supported CFR30 season workflow."""
from pathlib import Path
from .common import load, sha


def unsealed(root):
    root = Path(root)
    if (root/'completion-status.json').exists():
        raise ValueError('Run is sealed; create a new versioned run')
    if (root/'delivery.json').exists():
        target = Path(load(root/'delivery.json')['video']).parent
        if (target/'SHA256SUMS.json').exists():
            raise ValueError('Delivery is sealed; create a new versioned run')


def episode_map(run, episodes):
    run = Path(run)
    path = run/'episode-map.json'
    mapping = load(path) if path.exists() else {str(ep):str(run/'episode-stores'/f'ep{ep:03d}') for ep in episodes}
    if set(mapping) != {str(ep) for ep in episodes}:
        raise ValueError('Video episode map scope differs')
    return mapping


def name_ids(episodes):
    return {c['cue_id'] for e in episodes for c in e['cues'] if c.get('proper_names')}


def name_decisions(root, cues):
    decisions = load(Path(root)/'name-decisions.json')
    expected = 'APPLIED_BEFORE_TTS' if any(c.get('proper_names') for c in cues) else 'NOT_REQUIRED'
    if decisions.get('status') != expected:
        raise ValueError('Name decision status differs from actual proper-name scope')
    scope=load(Path(root)/'manifest.json')['episodes']
    canonical={str(Path(root)/'translation'/f'episode{ep:03d}.json') for ep in scope}
    if set(decisions['canonical_snapshot'])!=canonical:
        raise ValueError('Name decision canonical snapshot coverage differs')
    for path, expected_hash in decisions['canonical_snapshot'].items():
        if sha(path) != expected_hash:
            raise ValueError('Canonical changed after name decisions')
    return decisions


def validate_review(review, episodes, generation):
    expected = name_ids(episodes)
    if review.get('video_generation') != generation:
        raise ValueError('Name review video generation is stale')
    if not expected:
        if review.get('status') != 'NOT_REQUIRED' or review.get('items') != []:
            raise ValueError('Zero-name scope requires NOT_REQUIRED with no review items')
        return
    if review.get('status') != 'PASS' or {i['cue_id'] for i in review['items']} != expected or len(review['items']) != len(expected):
        raise ValueError('Actual name review coverage incomplete')
    checks={'chinese_covered','english_correct','no_clipping','max_two_lines','position_correct'}
    for item in review['items']:
        if (item.get('status')!='PASS' or set(item.get('checks',{}))!=checks
                or any(item['checks'][key] is not True for key in checks)
                or any(not isinstance(item.get(key),str) or not item[key].strip() for key in ['reviewer','note'])):
            raise ValueError('Name review contains an unapproved item')
        for key in ['image','contact']:
            path=item.get(key);expected_hash=item.get(key+'_sha256')
            if not path or not expected_hash or not Path(path).is_file() or sha(path)!=expected_hash:
                raise ValueError('Name review evidence missing or changed: '+key)
