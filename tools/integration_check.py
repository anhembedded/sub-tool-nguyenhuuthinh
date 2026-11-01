"""Synthetic controller integration using real FFmpeg, never film approval.

Catalog, saved recognition, Edge service and MVSEP are explicit deterministic
fixtures. Native source/render/fit/mix/final decode/SRT/seal operations are real.
Use smoke.py separately to validate actual GPU engines and the online service.
"""
import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / 'tools'))
from hongguo_lite.common import atomic, config, load, sha, utc
from hongguo_lite import source, media
from hongguo_lite.store import Store
import season_editorial
import season_name_ocr
import season_visual_review
import season_speech
import season_media
import season_pts
import season_finish
import season_package
import season_delivery_audit

QUALIFICATION = 'Synthetic controller integration; generated tones and known-layout fixtures; not film, listening or semantic approval.'


def execute(argv, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    args = list(map(str, argv))
    start = time.monotonic()
    with (folder / f'{name}.stdout.log').open('wb') as out, (folder / f'{name}.stderr.log').open('wb') as err:
        result = subprocess.run(args, stdout=out, stderr=err, timeout=600,
                                env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    atomic(folder / f'{name}.json', dict(argv=args, exit_code=result.returncode,
           seconds=time.monotonic() - start, qualification=QUALIFICATION))
    if result.returncode:
        raise RuntimeError(f'Synthetic command {name} failed; inspect {folder / (name + ".stderr.log")}')


def _float_fixture(path, samples):
    import numpy as np
    mono = (np.sin(np.arange(samples) * 2 * np.pi * 440 / 48000) * .025).astype('<f4')
    payload = np.column_stack([mono, mono]).astype('<f4').tobytes()
    path.write_bytes(b'RIFF' + struct.pack('<I', 36 + len(payload)) + b'WAVEfmt '
                     + struct.pack('<IHHIIHH', 16, 3, 2, 48000, 384000, 8, 32)
                     + b'data' + struct.pack('<I', len(payload)) + payload)


def source_fixture(root, total, env, preset):
    from PIL import Image, ImageDraw, ImageFont
    root.mkdir(parents=True)
    marker = dict(qualification=QUALIFICATION, real_film=False, generated_fixture=True)
    atomic(root / 'SYNTHETIC-FIXTURE.json', marker)
    poster = root / 'poster.png'
    image = Image.new('RGB', (1920, 1080), '#203040' if total == 2 else '#403020')
    ImageDraw.Draw(image).text((110, 895), f'SYNTHETIC SERIES {total}', font=ImageFont.truetype(env['font'], 48), fill='white')
    image.save(poster)
    native = root / 'generated-native.mp4'
    execute([env['ffmpeg'], '-v', 'error', '-nostdin', '-n', '-loop', '1', '-framerate', '30', '-i', poster,
             '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000', '-map', '0:v:0', '-map', '1:a:0',
             '-frames:v', '30', '-t', '1', '-c:v', 'libx265', '-preset', 'ultrafast',
             '-x265-params', 'pools=1:frame-threads=2:log-level=error', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-ar', '48000', '-ac', '2', native], root / 'fixture-logs', 'source')
    execute([env['ffmpeg'], '-v', 'error', '-xerror', '-nostdin', '-i', native, '-f', 'null', '-'],
            root / 'fixture-logs', 'source-full-decode')
    probe = json.loads(subprocess.check_output([env['ffprobe'], '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(native)], text=True))
    src = source.probe(native, env)
    if (src['frames'], src['fps'], src['width'], src['height']) != (30, '30', 1920, 1080):
        raise ValueError('Generated source does not match the native profile')
    series_id = f'910000000000000{total}'
    rows = []
    for ep in range(1, total + 1):
        path = root / 'download' / 'sources' / f'ep{ep:03d}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(native, path)
        rows.append(dict(episode=ep, vid=f'{series_id}{ep}', path=str(path), status='ready',
                         validation=dict(sha256=sha(path), frameCount=30, frameRate='30',
                                         fullDecodeExitCode=0, probe=probe), synthetic_fixture=True))
    atomic(root / 'download/download_report.json', dict(status='complete', seriesId=series_id,
           seriesTitle=f'SYNTHETIC INTEGRATION {total} EPISODES; NOT A FILM',
           seriesEpisodeCount=total, episodes=rows, qualification=QUALIFICATION))
    atomic(root / 'manifest.json', dict(schema=1, series_id=series_id, series_title=f'SYNTHETIC INTEGRATION {total} EPISODES; NOT A FILM',
           episodes=list(range(1, total + 1)), status='READY_FOR_CONTEXT_TRANSLATION',
           environment=env, latest_export=str(root / 'export-fixture'), qualification=QUALIFICATION))
    atomic(root / 'preset.json', preset)
    atomic(root / 'runtime-lock.json', dict(schema=1, files={}, scope='Synthetic controller fixture only; no engine acceptance.'))
    atomic(root / 'workflow-lock.json', dict(files={}, scope='Synthetic controller fixture only.'))
    atomic(root / 'config.json', dict(voice='en-US-AvaMultilingualNeural', rate='+0%', min_tempo=1.25,
           max_tempo=5, ffmpeg=env['ffmpeg'], python=env['edge_python']))
    export = root / 'export-fixture'
    export.mkdir()
    atomic(export / 'saved-draft.json', dict(qualification=QUALIFICATION, simulated_saved_recognition=True))
    bounds = []
    srt = []
    for ep in range(1, total + 1):
        bounds.append(dict(episode=ep, start_us=(ep - 1) * 1000000, end_us=ep * 1000000))
        atomic(export / f'episode{ep:03d}.json', dict(episode=ep, cues=[dict(cue_id=ep,
               source_text=f'SYNTHETIC SOURCE {ep}', start_us=100000, end_us=700000)]))
        srt.append(f'{ep}\n00:00:{ep - 1:02d},100 --> 00:00:{ep - 1:02d},700\nSYNTHETIC SOURCE {ep}')
    (export / 'raw.zh.srt').write_text('\n\n'.join(srt) + '\n', encoding='utf-8')
    atomic(export / 'extraction-report.json', dict(status='READY_FOR_CONTEXT_TRANSLATION',
           cue_count=total, boundaries=bounds, anomalies=[], timeline_anomalies=[], missing_episodes=[],
           series_id=series_id, source_report_sha256=sha(root / 'download/download_report.json'),
           sources={str(r['episode']): r['validation']['sha256'] for r in rows},
           draft_sha256=sha(export / 'saved-draft.json'), raw_srt_sha256=sha(export / 'raw.zh.srt'),
           files={p.name: sha(p) for p in export.iterdir()}, qualification=QUALIFICATION))
    tone = root / 'fixture-edge-response.mp3'
    execute([env['ffmpeg'], '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=660:sample_rate=48000:duration=0.24', '-ac', '1', '-c:a', 'libmp3lame', tone],
            root / 'fixture-logs', 'edge-response-tone')
    return tone


def background_fixture(root):
    m = load(root / 'manifest.json')
    env = m['environment']
    report = root / 'download/download_report.json'
    store = Store(root / 'media-store')
    for row in load(report)['episodes']:
        ep = row['episode']
        source_row = dict(episode=ep, path=row['path'], sha256=row['validation']['sha256'],
                          adapter_validation=dict(report=str(report), report_sha256=sha(report)))
        src = store.stage(f'ep{ep:03d}/source', dict(fixture_source=source_row),
                          lambda out: source.validate(source_row, out, env))
        def separate(out):
            wav = out / 'fixture-sfx-float48.wav'
            _float_fixture(wav, src['frames'] * 1600)
            response = out / 'response.json'
            atomic(response, dict(technical_status='PASS', synthetic_fixture=True,
                   engine='DETERMINISTIC_FLOAT_TONE_FIXTURE', qualification=QUALIFICATION,
                   included_stems=['sfx'], excluded_stems=['music', 'speech'],
                   input_samples=src['frames'] * 1600, output_samples=src['frames'] * 1600,
                   output=str(wav), output_sha256=sha(wav), listening_approval='NOT_PERFORMED'))
            return dict(output=str(wav), sha256=sha(wav), receipt=str(response),
                        included_stems=['sfx'], excluded_stems=['music', 'speech'],
                        technical_PASS=True, synthetic_fixture=True, files=[str(wav), str(response)])
        store.stage(f'ep{ep:03d}/separation', dict(synthetic_fixture=True, source=src['sha256'], included_stems=['sfx']), separate)


def run_series(root, total, env, preset):
    stages = []
    def stage(action, function):
        tick = time.monotonic()
        record = dict(action=action, begin_utc=utc(), qualification=QUALIFICATION,
                      invocation='Worker function used by tools/season.py')
        try:
            result = function()
            record['status'] = 'COMPLETE'
            return result
        except Exception as exc:
            record.update(status='FAILED', error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            record.update(seconds=time.monotonic() - tick, end_utc=utc())
            stages.append(record)
            atomic(root / 'executions' / f'{action}-{time.time_ns()}' / 'result.json', record)
    tone = source_fixture(root, total, env, preset)
    stage('editorial-export', lambda: season_editorial.editorial_export(root))
    draft = load(root / 'editorial-draft.json')
    draft.update(reviewer='Synthetic fixture generator; not a film reviewer',
                 evidence=[dict(path=str(root / 'SYNTHETIC-FIXTURE.json'), sha256=sha(root / 'SYNTHETIC-FIXTURE.json'))])
    for cue in draft['cues']:
        cue.update(action='KEEP', classification='DIALOGUE', canonical_text=f'Synthetic {total} episode {cue["episode"]}.', proper_names=[])
    atomic(root / 'editorial-draft.json', draft)
    stage('editorial-import', lambda: season_editorial.editorial_import(root, root / 'editorial-draft.json'))
    geometry = stage('geometry-prepare', lambda: season_editorial.geometry_prepare(root))
    document = load(geometry['draft'])
    document['reviewer'] = 'Known generated fixture geometry assertion; no film review'
    for item in document['episodes'].values():
        item.update(reviewed=True, note=QUALIFICATION,
                    blur_box=dict(x=64, y=880, width=1792, height=120))
    atomic(geometry['draft'], document)
    stage('geometry-import', lambda: season_editorial.geometry_import(root, geometry['draft']))
    stage('ocr', lambda: season_name_ocr.run(root))
    stage('names-import', lambda: season_editorial.names_import(root))
    class FixtureCommunicate:
        def __init__(self, *args, **kwargs):
            pass
        async def save(self, destination):
            shutil.copy2(tone, destination)
    version = importlib.metadata.version
    def fixture_version(name):
        return 'synthetic-tone-fixture-no-edge-service' if name == 'edge-tts' else version(name)
    with patch.dict(sys.modules, {'edge_tts': SimpleNamespace(Communicate=FixtureCommunicate)}), \
         patch.object(importlib.metadata, 'version', side_effect=fixture_version):
        stage('speech', lambda: asyncio.run(season_speech.synthesize(root)))
    stage('background', lambda: background_fixture(root))
    m = load(root / 'manifest.json')
    stage('video', lambda: season_media.video(root, m, env, preset))
    stage('review-prepare', lambda: season_visual_review.prepare(root))
    stage('pts', lambda: season_pts.main(root))
    stage('finish', lambda: season_finish.main(root))
    delivered = stage('package', lambda: season_package.main(root))
    stage('seal', lambda: season_delivery_audit.main(root))
    for action in ['status', 'resume']:
        execute([env['controller_python'], '-B', '-X', 'utf8', PROJECT / 'tools/season.py', action, '--run', root],
                root / 'fixture-logs', f'entrypoint-{action}')
    payload = load(Path(delivered['video']).parent / 'SHA256SUMS.json')
    if any(sha(Path(delivered['video']).parent / name) != expected for name, expected in payload['files'].items()):
        raise ValueError('Sealed synthetic delivery changed')
    expected = dict(episodes=list(range(1, total + 1)), frames=30 * total,
                    pcm_samples=48000 * total, cue_count=total, seams=total - 1)
    if any(delivered[k] != value for k, value in expected.items()):
        raise ValueError('Synthetic scope/frame/sample/cue/seam counts differ')
    if (delivered['name_review_status'] != 'NOT_REQUIRED' or delivered['listening_status'] != 'NOT_PERFORMED'
            or delivered['semantic_status'] != 'NOT_PERFORMED' or delivered['external_published'] is not False):
        raise ValueError('Synthetic run must not claim actual name/listening/semantic/publication approval')
    return dict(series_id=m['series_id'], status='PASS', qualification=QUALIFICATION,
                expected=expected, actual={k: delivered[k] for k in expected},
                name_review_status='NOT_REQUIRED', delivery=str(Path(delivered['video']).parent),
                video_sha256=delivered['video_sha256'], srt_sha256=delivered['srt_sha256'],
                stages=stages, controller_status_and_resume_exit_codes=0,
                sealed_payloads_verified=True)


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    out = a.output.resolve()
    if out.exists():
        raise FileExistsError('Preserve previous integration evidence; choose a new output directory')
    if out.is_relative_to(PROJECT):
        raise ValueError('Integration fixtures must live outside the shipped source release')
    cfg = config(a.config)
    env = {**cfg['env'], 'deliveries_root': str(out / 'deliveries')}
    preset = load(cfg.get('preset', PROJECT / 'config/preset.json'))
    preset['separation']['included_stems'] = ['sfx']
    preset['video'].update(preset='ultrafast', threads=2, filter_threads=1, crf=28)
    out.mkdir(parents=True)
    tick = time.monotonic()
    report = dict(schema=1, qualification=QUALIFICATION, status='RUNNING',
                  engine_acceptance='NOT_PROVEN_BY_THIS_CHECK', real_film_approval=False,
                  fixtures=['catalog', 'saved CapCut recognition', 'Edge response tone', 'MVSEP float SFX'],
                  config_sha256=sha(a.config), begin_utc=utc(), series=[])
    try:
        for total in [2, 3]:
            report['series'].append(run_series(out / f'synthetic-series-{total}', total, env, preset))
            atomic(out / 'integration.json', report)
        report['status'] = 'PASS_SYNTHETIC_CONTROLLER_INTEGRATION'
    except Exception as exc:
        report.update(status='FAILED', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        report.update(end_utc=utc(), seconds=time.monotonic() - tick)
        atomic(out / 'integration.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
