"""Inspect the configured real engines; offline success still requires smoke QA."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from hongguo_lite.common import config, atomic, load, sha, process_lock, utc

ROLES = ('controller', 'edge', 'paddle', 'separation', 'capcut')
REQUIRED_FILES = ('controller_python', 'edge_python', 'paddle_python', 'separation_python', 'capcut_python',
    'node', 'ffmpeg', 'ffprobe', 'font', 'preview_font', 'logo', 'wordmark',
    'separation_checkpoint', 'separation_engine_lock', 'cupti_library')


def probe(python, role, env):
    """Each role imports in its own process; incompatible CUDA DLLs never mix."""
    code = r'''
import sys,json,os,importlib.metadata as m
from pathlib import Path
role=sys.argv[1]; env=json.loads(sys.argv[2]); handles=[]
sys.path.insert(0,sys.argv[3]); sys.path.insert(0,str(Path(sys.argv[3])/'tools'))
if os.name=='nt':
    dirs=env.get('ocr_dll_directories' if role=='paddle' else 'separation_dll_directories',[]) if role in ['paddle','separation'] else []
    handles=[os.add_dll_directory(str(p)) for p in dirs if Path(p).is_dir()]
    os.environ['PATH']=os.pathsep.join(dirs)+os.pathsep+os.environ.get('PATH','')
if role=='controller':
    import numpy,PIL,srt
    from hongguo_lite import media
elif role=='edge':
    import edge_tts,aiohttp
    from hongguo_lite import edge_fit
    import season_speech
elif role=='capcut':
    import pycapcut,pymediainfo
    from pycapcut.assets import get_asset_path
    assert pymediainfo.MediaInfo.can_parse(), 'Native MediaInfo unavailable'
    assert get_asset_path('DRAFT_CONTENT_TEMPLATE').is_file()
    assert get_asset_path('DRAFT_META_TEMPLATE').is_file()
elif role=='paddle':
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
    import paddle
    from paddleocr import PaddleOCR
    assert paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count()>0, 'Paddle GPU unavailable'
    paddle.device.set_device('gpu:0'); x=paddle.ones([16,16],dtype='float32'); result=paddle.matmul(x,x)
    assert result.dtype==paddle.float32 and float(result[0,0])==16, 'Paddle FP32 operation failed'
elif role=='separation':
    import torch,torchaudio,demucs,soundfile,scipy,ctypes
    assert torch.cuda.is_available(), 'Torch CUDA unavailable'
    x=torch.ones((16,16),device='cuda',dtype=torch.float32); result=x@x; torch.cuda.synchronize()
    assert result.is_cuda and result.dtype==torch.float32 and float(result[0,0])==16, 'Torch FP32 operation failed'
    ctypes.WinDLL(env['cupti_library'])
print(json.dumps({'role':role,'python':sys.version.split()[0],'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata.get('Name')},'imports':'PASS','gpu_fp32_operation':'PASS' if role in ['paddle','separation'] else 'NOT_APPLICABLE'}))
'''
    process = subprocess.run([str(python), '-B', '-X', 'utf8', '-c', code, role, json.dumps(env), str(ROOT)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'})
    if process.returncode:
        raise RuntimeError(f'{role} actual engine probe failed: {process.stderr[-3500:]}')
    # Libraries may print notices; our last stdout line is the measured payload.
    result = json.loads(process.stdout.strip().splitlines()[-1])
    if not result['python'].startswith('3.12.'):
        raise ValueError(f'{role} must run CPython 3.12')
    lock = ROOT / 'config/locks' / f'{role}.archives.json'
    if not lock.exists(): raise ValueError(f'Missing release closure lock {lock}')
    for expected in load(lock)['packages']:
        name = expected['name'].lower().replace('_', '-')
        if result['packages'].get(name) != expected['version']:
            raise ValueError(f'{role} package differs from release lock: {name}')
    return result


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--online', action='store_true')
    args = parser.parse_args(args)
    output = (args.output or args.config.resolve().parent / 'doctor' / str(time.time_ns())).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'status': 'RUNNING', 'begin_utc': utc(), 'config_sha256': sha(args.config),
        'checks': [], 'capcut_recognition': 'MANUAL_CHINESE_AUTO_CAPTIONS_AND_SAVE_REQUIRED', 'source_service': 'NOT_CONTACTED'}
    try:
        cfg = config(args.config); env = cfg['env']
        for key in REQUIRED_FILES:
            path = Path(env[key])
            if not path.is_file(): raise FileNotFoundError(f'{key}: {path}')
        for key in ('ocr_det_model', 'ocr_rec_model'):
            for name in ('inference.json', 'inference.pdiparams', 'inference.yml'):
                if not (Path(env[key]) / name).is_file(): raise FileNotFoundError(f'{key}/{name}')
        manifest = load(env['runtime_manifest'])
        for path, expected in manifest['files'].items():
            if sha(path) != expected: raise ValueError(f'Runtime payload changed: {path}')
        report['checks'].append({'name': 'runtime_payload_hashes', 'status': 'PASS', 'files': len(manifest['files'])})
        report['engines'] = {}
        for role in ROLES:
            if role in ('paddle', 'separation'):
                with process_lock(env['gpu_lock']): report['engines'][role] = probe(env[role + '_python'], role, env)
            else: report['engines'][role] = probe(env[role + '_python'], role, env)
        version = subprocess.check_output([env['node'], '--version'], text=True, timeout=30).strip()
        if tuple(map(int, version.lstrip('v').split('.')[:2])) < (22,12): raise ValueError('Node >=22.12 required')
        axios = subprocess.check_output([env['node'], '-e', "const p=require(process.argv[1]); if(typeof p.get!=='function')process.exit(2);console.log(p.VERSION)",
            str(ROOT / 'vendor/core/source_adapter/node_modules/axios')], text=True, timeout=30).strip()
        if axios != '1.20.0': raise ValueError('Axios version differs from lock')
        for executable in ('ffmpeg', 'ffprobe'):
            subprocess.run([env[executable], '-version'], check=True, capture_output=True, timeout=30)
        filters = subprocess.check_output([env['ffmpeg'], '-hide_banner', '-filters'], text=True, stderr=subprocess.STDOUT, timeout=30)
        encoders = subprocess.check_output([env['ffmpeg'], '-hide_banner', '-encoders'], text=True, stderr=subprocess.STDOUT, timeout=30)
        for name in ['overlay', 'boxblur', 'atempo', 'aresample', 'apad', 'atrim', 'amix', 'alimiter', 'showinfo', 'select', 'concat']:
            if not any(line.split()[1:2] == [name] for line in filters.splitlines()): raise ValueError(f'FFmpeg filter missing: {name}')
        for name in ['libx264', 'aac', 'pcm_s16le', 'pcm_f32le']:
            if not any(line.split()[1:2] == [name] for line in encoders.splitlines()): raise ValueError(f'FFmpeg encoder missing: {name}')
        report['checks'].append({'name': 'node_axios_ffmpeg', 'status': 'PASS', 'node': version, 'axios': axios})
        report['status'] = 'ENGINE_IMPORTS_PASS_SMOKE_REQUIRED'
        if args.online:
            target = output / 'smoke'
            subprocess.run([env['controller_python'], '-B', str(ROOT / 'tools/smoke.py'), '--config', str(args.config), '--output', str(target), '--online'], check=True, timeout=7200)
            smoke = load(target / 'smoke.json')
            if smoke['status'] != 'PASS' or smoke['config_sha256'] != sha(args.config) or smoke.get('online') is not True: raise ValueError('Actual online smoke missing/stale')
            report.update(status='READY_ENGINE_SMOKE_PASS', smoke=str(target / 'smoke.json'))
    except BaseException as exc:
        report.update(status='FAILED', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        report['end_utc'] = utc(); atomic(output / 'doctor.json', report)
    print(json.dumps({'status': report['status'], 'report': str(output / 'doctor.json')}, ensure_ascii=False))
    return report


if __name__ == '__main__': main()
