"""Isolated season source/draft gate. Never performs CapCut recognition."""
import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
import os
from fractions import Fraction
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from hongguo_lite.common import atomic, digest, load, sha, utc, process_lock, config


def episodes(value):
    first, last = map(int, value.split('-'))
    if not 1 == first <= last <= 9999:
        raise ValueError('Stable profile requires the whole series: episodes 1-N')
    return list(range(first, last + 1))


def stamp(us):
    ms = round(us / 1000)
    sec, ms = divmod(ms, 1000)
    minute, sec = divmod(sec, 60)
    hour, minute = divmod(minute, 60)
    return f'{hour:02}:{minute:02}:{sec:02},{ms:03}'


def draft_name(root, manifest):
    """A unique draft for the entire run identity, not just a version suffix."""
    identity=digest(dict(run=str(Path(root).resolve()),series=manifest['series_id'],episodes=manifest['episodes']))[:12]
    return f"CC_SRT_{manifest['series_id']}_EP001-{manifest['episodes'][-1]:03d}_{identity}"


def caption_cues(data):
    """Read saved plaintext CapCut captions; this function never performs ASR."""
    materials={m['id']:m for m in data.get('materials',{}).get('texts',[]) if 'id' in m}
    tracks=[]
    for index,track in enumerate(data.get('tracks',[])):
        if track.get('type')!='text':continue
        cues=[]
        for segment in track.get('segments',[]):
            material=materials.get(segment.get('material_id'))
            if not material:continue
            content=material.get('content','')
            if isinstance(content,str):
                try:content=json.loads(content)
                except (ValueError,TypeError):pass
            text=content.get('text','') if isinstance(content,dict) else content
            if not isinstance(text,str) or not text.strip():continue
            timerange=segment.get('target_timerange',{})
            start,duration=timerange.get('start'),timerange.get('duration')
            if type(start) is not int or type(duration) is not int or start<0 or duration<=0:
                raise ValueError('Caption timerange must be positive integer microseconds')
            cues.append((start,start+duration,text.strip()))
        if cues:tracks.append((index,track.get('name',''),sorted(cues)))
    return tracks


def partition(cues, bounds):
    """Never duplicate or clip a crossing cue; retain it in anomalies."""
    parts = {r['episode']: [] for r in bounds}
    anomalies = []
    seen = set()
    previous_end = 0
    for index, (a, b, text) in enumerate(cues, 1):
        reasons = []
        matches = [r for r in bounds if r['start_us'] <= a < b <= r['end_us']]
        if len(matches) != 1:
            reasons.append('CROSSING_OR_OUTSIDE_BOUNDARY')
        if b - a < 100000:
            reasons.append('SHORT_CUE')
        if (a, b, text) in seen:
            reasons.append('DUPLICATE')
        if a < previous_end:
            reasons.append('OVERLAP')
        if reasons:
            anomalies.append(dict(cue_id=index, start_us=a, end_us=b, text=text, reasons=reasons, disposition='NEEDS_REVIEW'))
        if len(matches) == 1:
            r = matches[0]
            parts[r['episode']].append(dict(cue_id=index, start_us=a-r['start_us'], end_us=b-r['start_us'], source_text=text))
        seen.add((a, b, text))
        previous_end = max(previous_end, b)
    return parts, anomalies


def init(args):
    root = args.run.resolve()
    if root.exists():
        raise FileExistsError('Run already exists; use its explicit resume commands')
    cfg = config(getattr(args,'config',None) or os.environ.get('WORKFLOW_CONFIG',PROJECT/'config/local.json'))
    env = cfg['env']
    required = ['node', 'ffmpeg', 'ffprobe', 'font', 'logo', 'wordmark', 'separation_checkpoint']
    for key in required:
        if not Path(env[key]).is_file():
            raise FileNotFoundError(env[key])
    preset = load(cfg.get('preset',PROJECT/'config/preset.json'))
    preset['mix']['background_db'] = -5
    preset['fit'] = dict(backend='edge-tts', voice='en-US-AvaMultilingualNeural', native_rate='+0%', tempo_min=1.25, tempo_max=5.0, algorithm='continuous_measured_pcm')
    preset['separation']['included_stems']=['sfx']
    preset['separation']['excluded_stems']=['music','speech']
    root.mkdir(parents=True)
    atomic(root/'preset.json', preset)
    atomic(root/'config.json', dict(voice='en-US-AvaMultilingualNeural',rate='+0%',min_tempo=1.25,max_tempo=5,ffmpeg=env['ffmpeg'],python=env['edge_python']))
    manifest = dict(schema=1, created_utc=utc(), series_id=args.series_id, episodes=episodes(args.episodes), status='INITIALIZED',
        backend=dict(asr='MANUAL_CAPCUT_CHINESE', tts='edge-tts', voice='en-US-AvaMultilingualNeural', native_rate='+0%'),
        policy=dict(background_db=-5, fit_min=1.25, fit_max=5, geometry='per_episode_from_native_chinese_caption_evidence_during_name_OCR', delivery='single_mp4_and_combined_srt', no_audio_tail_work=True),
        acceptance=dict(artifact_ready=False, technical_pass=False, name_qa_pass=False, listening_status='NOT_PERFORMED', semantic_status='NOT_PERFORMED', external_published=False),
        dependencies={str(p): sha(p) for p in [Path(__file__), Path(cfg['_path']), root/'config.json', Path(env['font']), Path(env['logo']), Path(env['wordmark']), root/'preset.json']},
        environment=env, capcut_python=env['capcut_python'], configuration=cfg['_path'],
        next_action='preflight')
    atomic(root/'manifest.json', manifest)
    atomic(root/'download-config.json', dict(seriesId=args.series_id, episodes=manifest['episodes'], outputRoot=str(root/'download'), concurrency=2, ffmpeg=env['ffmpeg'], ffprobe=env['ffprobe']))
    print(str(root))


def sources(root, m):
    report_path = root/'download/download_report.json'
    report = load(report_path)
    if report.get('status') != 'complete' or report['seriesId'] != m['series_id']:
        raise ValueError('Download incomplete or wrong series')
    if report['seriesEpisodeCount'] != len(m['episodes']):
        raise ValueError('Catalog episode count differs from requested whole-season scope')
    rows = sorted(report['episodes'], key=lambda r: r['episode'])
    if [r['episode'] for r in rows] != m['episodes'] or len({r['vid'] for r in rows}) != len(rows):
        raise ValueError('Episode scope/identity mismatch')
    for r in rows:
        if r['status'] != 'ready' or r['validation']['fullDecodeExitCode'] != 0 or sha(r['path']) != r['validation']['sha256']:
            raise ValueError(f"Invalid source {r['episode']}")
        videos=[s for s in r['validation'].get('probe',{}).get('streams',[]) if s.get('codec_type')=='video']
        if len(videos)!=1:raise ValueError('Source profile probe missing or ambiguous')
        video=videos[0]
        if (video.get('codec_name'),video.get('width'),video.get('height'),video.get('pix_fmt'))!=('hevc',1920,1080,'yuv420p'):
            raise ValueError('Supported source profile is exact 1920x1080 HEVC SDR yuv420p')
        if (Fraction(video.get('avg_frame_rate','0'))!=30 or Fraction(video.get('r_frame_rate','0'))!=30
                or video.get('sample_aspect_ratio','1:1') not in ['1:1','N/A']
                or video.get('color_transfer') in ['smpte2084','arib-std-b67']):
            raise ValueError('Supported source profile requires native CFR30 SDR square pixels')
    return report, rows


def download(root, m):
    if (root/'completion-status.json').exists():raise ValueError('Completed run is immutable; create a new run')
    existing=root/'download/download_report.json'
    if existing.exists() and load(existing).get('status')=='complete':
        report,_=sources(root,m)
        if not m.get('series_title'):
            m.update(series_title=report['seriesTitle'],status='SOURCE_VERIFIED',source_report_sha256=sha(existing),next_action='prepare')
            atomic(root/'manifest.json',m)
        print('Existing verified download preserved; use prepare or the next recorded checkpoint')
        return
    env = m['environment']
    tick = time.monotonic()
    result = subprocess.run([env['node'], str(Path(env['source_adapter'])/'cli.cjs'), 'download', '--config', str(root/'download-config.json')])
    atomic(root/f'download-execution-{time.time_ns()}.json', dict(seconds=time.monotonic()-tick, exit_code=result.returncode, completed_utc=utc()))
    if result.returncode:
        m.update(status='SOURCE_BLOCKED', next_action='Inspect download/download_report.json; preserve failed evidence')
        atomic(root/'manifest.json', m)
        raise RuntimeError('Source adapter failed; see sanitized download report')
    report, rows = sources(root, m)
    m.update(status='SOURCE_VERIFIED', series_title=report['seriesTitle'], source_report_sha256=sha(root/'download/download_report.json'), next_action='prepare')
    atomic(root/'manifest.json', m)


def prepare(root, m):
    import pycapcut as cc
    report, rows = sources(root, m)
    folder = Path(m['environment']['capcut_project_root'])
    name = draft_name(root,m)
    dest = folder/name
    if dest.exists():
        raise FileExistsError(f'Preserve existing draft {dest}')
    mats = [cc.VideoMaterial(r['path']) for r in rows]
    if len({(v.width, v.height) for v in mats}) != 1:
        raise ValueError('Mixed source dimensions require review')
    folder.mkdir(parents=True, exist_ok=True)
    draft = cc.DraftFolder(str(folder)).create_draft(name, mats[0].width, mats[0].height, fps=30)
    draft.add_track(cc.TrackType.video)
    cursor = 0
    bounds = []
    for r, mat in zip(rows, mats):
        duration=round(Fraction(r['validation']['frameCount']*1000000,30))
        if mat.duration < duration-1000:raise ValueError('CapCut material shorter than verified native video clock')
        draft.add_segment(cc.VideoSegment(mat, cc.Timerange(cursor, duration)))
        bounds.append(dict(episode=r['episode'], source=r['path'], source_sha256=r['validation']['sha256'], frames=r['validation']['frameCount'], start_us=cursor, end_us=cursor+duration))
        cursor += duration
    draft.save()
    meta = load(dest/'draft_meta_info.json')
    meta.update(draft_id=str(uuid.uuid4()).upper(), draft_name=name, draft_fold_path=str(dest), draft_root_path=str(folder), tm_duration=cursor, tm_draft_create=int(time.time()*1e6), tm_draft_modified=int(time.time()*1e6))
    atomic(dest/'draft_meta_info.json', meta)
    content = load(dest/'draft_content.json')
    assert len(content['tracks'][0]['segments']) == len(rows) and not content['materials']['texts']
    shutil.copyfile(dest/'draft_content.json', root/'prepared-draft.json')
    atomic(root/'boundaries.json', dict(name=name, draft=str(dest/'draft_content.json'), episodes=bounds, total_us=cursor, source_report_sha256=sha(root/'download/download_report.json'), prepared_draft_sha256=sha(root/'prepared-draft.json'), pycapcut=cc.__file__))
    m.update(status='AWAITING_MANUAL_AUTO_CAPTIONS', series_title=report['seriesTitle'], draft_name=name, total_seconds=cursor/1e6, next_action='Open draft in CapCut, run Auto captions Chinese, save, then extract')
    atomic(root/'manifest.json', m)
    print(json.dumps(dict(status=m['status'], draft=name, episodes=len(rows), total_seconds=cursor/1e6), ensure_ascii=True))


def extract(root, m):
    sources(root, m)
    boundary = load(root/'boundaries.json')
    path = Path(boundary['draft'])
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    tracks = caption_cues(data)
    if len(tracks) != 1:
        raise ValueError('Expected exactly one saved caption track; run manual recognition and save first')
    videos = sorted([s for t in data['tracks'] if t.get('type') == 'video' for s in t['segments']], key=lambda s:s['target_timerange']['start'])
    materials = {v['id']:v for v in data['materials']['videos']}
    if len(videos) != len(m['episodes']):
        raise ValueError('Source clip count changed')
    bounds = []
    timeline_anomalies = []
    cursor = 0
    for expected, seg in zip(boundary['episodes'], videos):
        tr, src = seg['target_timerange'], seg.get('source_timerange', {})
        if Path(materials[seg['material_id']]['path']).resolve() != Path(expected['source']).resolve():
            raise ValueError('Source order/path changed')
        if src.get('start',0) != 0 or seg.get('speed',1) != 1 or abs(tr['start']-cursor)>1 or abs(tr['duration']-(expected['end_us']-expected['start_us']))>1000 or abs(src.get('duration',tr['duration'])-tr['duration'])>1:
            timeline_anomalies.append(dict(episode=expected['episode'], reason='TIMELINE_DIFFERS_FROM_PREPARED_SOURCE', expected=expected, saved_target=tr, saved_source=src, speed=seg.get('speed',1), disposition='NEEDS_REVIEW'))
        cursor = tr['start']+tr['duration']
        bounds.append(dict(episode=expected['episode'], start_us=tr['start'], end_us=cursor))
    cues = tracks[0][2]
    parts, anomalies = partition(cues, bounds)
    missing = [ep for ep, rows in parts.items() if not rows]
    out = root/f'export-{time.time_ns()}'
    out.mkdir()
    (out/'saved-draft.json').write_bytes(raw)
    (out/'raw.zh.srt').write_text('\n\n'.join(f'{i}\n{stamp(a)} --> {stamp(b)}\n{t}' for i,(a,b,t) in enumerate(cues,1))+'\n', encoding='utf-8-sig')
    for ep, rows in parts.items():
        atomic(out/f'episode{ep:03d}.json', dict(episode=ep, cues=rows))
        (out/f'episode{ep:03d}.zh.srt').write_text('\n\n'.join(f"{i}\n{stamp(r['start_us'])} --> {stamp(r['end_us'])}\n{r['source_text']}" for i,r in enumerate(rows,1))+'\n', encoding='utf-8-sig')
    status = 'NEEDS_SOURCE_REVIEW' if anomalies or missing or timeline_anomalies else 'READY_FOR_CONTEXT_TRANSLATION'
    atomic(out/'extraction-report.json', dict(status=status, boundaries=bounds, anomalies=anomalies, timeline_anomalies=timeline_anomalies, missing_episodes=missing, cue_count=len(cues),
        series_id=m['series_id'],source_report_sha256=sha(root/'download/download_report.json'),
        sources={str(r['episode']):r['validation']['sha256'] for r in load(root/'download/download_report.json')['episodes']},
        draft_sha256=sha(out/'saved-draft.json'), raw_srt_sha256=sha(out/'raw.zh.srt'), files={p.name:sha(p) for p in out.iterdir() if p.is_file()}))
    m.update(status=status, latest_export=str(out), next_action='Resolve anomalies then translate every cue with context; perform name OCR and per-episode geometry review before TTS')
    atomic(root/'manifest.json', m)
    print(str(out))


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('action', choices=['init','download','prepare','extract','status'])
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--series-id')
    p.add_argument('--episodes', help='Required explicit whole-season range for init')
    p.add_argument('--config',type=Path)
    args = p.parse_args()
    if args.action == 'init':
        if not args.series_id or not args.series_id.isdigit() or not args.episodes:
            p.error('init requires numeric --series-id and explicit --episodes FIRST-LAST')
        init(args)
        return
    root = args.run.resolve()
    with process_lock(root/'runner.lock'):
        m = load(root/'manifest.json')
        if args.action == 'status':
            print(json.dumps(load(root/'completion-status.json') if (root/'completion-status.json').exists() else m, ensure_ascii=True, indent=2))
        else:
            for path, expected in m['dependencies'].items():
                if sha(path) != expected:
                    raise ValueError(f'Dependency changed; create a new versioned run: {path}')
            globals()[args.action](root, m)


if __name__ == '__main__':
    main()
