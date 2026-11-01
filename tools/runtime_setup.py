"""Provision private Windows runtimes from release-pinned downloadable artifacts.

The base CPython 3.12 x64 and NVIDIA driver/VC++ runtime are host prerequisites.
Never installs into the calling Python, edits PATH, or reuses another project's venv.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hongguo_lite.common import atomic, load, sha, process_lock, utc

ROLES = ('controller', 'edge', 'capcut', 'paddle', 'separation')
PYTHON = '3.12'


def nvidia_driver():
    """Small Driver API check before downloading CUDA wheels or model payloads."""
    try:
        driver = ctypes.WinDLL('nvcuda.dll')
        driver.cuInit.argtypes = [ctypes.c_uint]; driver.cuInit.restype = ctypes.c_int
        driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]; driver.cuDeviceGetCount.restype = ctypes.c_int
        driver.cuDriverGetVersion.argtypes = [ctypes.POINTER(ctypes.c_int)]; driver.cuDriverGetVersion.restype = ctypes.c_int
        if driver.cuInit(0) != 0: raise RuntimeError('NVIDIA CUDA driver initialization failed')
        count = ctypes.c_int(); version = ctypes.c_int()
        if driver.cuDeviceGetCount(ctypes.byref(count)) != 0 or count.value < 1:
            raise RuntimeError('No NVIDIA CUDA device is available')
        if driver.cuDriverGetVersion(ctypes.byref(version)) != 0 or version.value < 11080:
            raise RuntimeError('NVIDIA driver must expose CUDA Driver API 11.8 or newer')
        return {'devices': count.value, 'cuda_driver_api': version.value, 'capacity': 'REAL_SMOKE_REQUIRED'}
    except (OSError, AttributeError, RuntimeError) as exc:
        raise RuntimeError(f'{exc}; install/enable a compatible NVIDIA GPU driver and rerun setup. No CPU fallback.') from exc


def run(argv, log, *, timeout=7200):
    log = Path(log); log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stream:
        stream.write(('\n$ ' + subprocess.list2cmdline(list(map(str, argv))) + '\n').encode())
        stream.flush()
        result = subprocess.run(list(map(str, argv)), stdout=stream, stderr=stream,
            timeout=timeout, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'})
    if result.returncode:
        raise RuntimeError(f'Command exited {result.returncode}; inspect {log}')


def download(item, cache):
    """HTTPS only, byte hash before use, unique partials; never trusts existence."""
    if not item['url'].startswith('https://') or len(item['sha256']) != 64:
        raise ValueError('Artifact needs an HTTPS source and exact SHA256')
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    target = cache / item.get('filename', item['sha256'])
    if target.exists():
        if sha(target) != item['sha256']:
            raise ValueError(f'Cached artifact changed: {target}; preserve it and select a clean runtime root')
        return target
    partial = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
    request = urllib.request.Request(item['url'], headers={'User-Agent': 'nguyenhuuthinh-workflow/1.1.0'})
    with urllib.request.urlopen(request, timeout=120) as source, partial.open('xb') as output:
        if not source.url.startswith('https://'):
            raise ValueError('Artifact redirected away from HTTPS')
        shutil.copyfileobj(source, output, 8 * 1024 * 1024)
    if sha(partial) != item['sha256']:
        raise ValueError(f'Download SHA256 mismatch; preserved {partial}')
    os.replace(partial, target)
    return target


def extract_zip(archive, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f'Archive target exists: {destination}')
    staging = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination) or entry.filename.startswith(('/', '\\')) or ':' in entry.filename:
                raise ValueError('Archive path escapes private runtime')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive symlink is not allowed')
        staging.mkdir(parents=True)
        package.extractall(staging)
    os.replace(staging, destination)


def verify_zip(archive, destination):
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            if entry.is_dir(): continue
            target = Path(destination) / entry.filename
            with package.open(entry) as stream:
                expected = hashlib.file_digest(stream, 'sha256').hexdigest()
            if not target.is_file() or sha(target) != expected:
                raise ValueError(f'Extracted tool changed/incomplete: {target}')


def role_files(target):
    return {p.relative_to(target).as_posix(): sha(p) for p in target.rglob('*') if p.is_file()
        and p.name != 'provisioned.json' and p.suffix != '.pyc' and '__pycache__' not in p.parts}


def query(python, code):
    return json.loads(subprocess.check_output([str(python), '-B', '-X', 'utf8', '-c', code], text=True, encoding='utf-8', timeout=180))


def environment_inventory(python):
    return query(python, "import sys,json,importlib.metadata as m,hashlib; print(json.dumps({'python':sys.version.split()[0],'distributions':{d.metadata['Name']:{'version':d.version,'record_sha256':hashlib.sha256((d.read_text('RECORD') or '').encode()).hexdigest()} for d in m.distributions() if d.metadata.get('Name')}}))")


def install_role(role, runtime, bootstrap, log, cache=None):
    target = (runtime / 'envs' / role).resolve()
    if not target.is_relative_to(runtime.resolve()):
        raise ValueError('Role directory escapes the private runtime root')
    python = target / 'Scripts/python.exe'
    marker = target / 'provisioned.json'
    pending = target / 'provisioning.json'
    requirements = ROOT / 'config/locks' / f'{role}.requirements.txt'
    build = ROOT / 'config/locks/build.requirements.txt'
    if not requirements.is_file() or not build.is_file():
        raise RuntimeError(f'Missing release requirement lock for {role}')
    identity = {'requirements_sha256': sha(requirements), 'build_sha256': sha(build)}
    if marker.exists():
        saved = load(marker)
        if saved['identity'] != identity or role_files(target) != saved['files']:
            raise ValueError(f'Runtime lock changed for {role}; choose a new --runtime-root')
        return python
    if target.exists() and not pending.exists():
        raise FileExistsError(f'Existing unowned role directory; preserve it and choose another runtime: {target}')
    if pending.exists() and load(pending) != identity:
        raise ValueError(f'Interrupted role uses another requirement lock: {target}')
    if not pending.exists(): atomic(pending, identity)
    if not python.exists():
        run([bootstrap, '-m', 'venv', target], log)
    # Build tooling is itself hash-locked before any source distribution executes.
    cache = Path(cache) if cache else runtime / 'lock-build/pip-cache'
    run([python, '-m', 'pip', 'install', '--cache-dir', cache, '--force-reinstall', '--require-hashes', '--no-deps', '-r', build], log)
    run([python, '-m', 'pip', 'install', '--cache-dir', cache, '--force-reinstall', '--require-hashes', '--no-deps', '--no-build-isolation', '-r', requirements], log)
    run([python, '-m', 'pip', 'check'], log)
    atomic(marker, {'identity': identity, 'inventory': environment_inventory(python), 'files': role_files(target), 'created_utc': utc()})
    return python


def find_one(root, name):
    matches = list(Path(root).rglob(name))
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one {name} under {root}; got {len(matches)}')
    return matches[0].resolve()


def prepare_config(runtime, paths, artifacts, output):
    node = find_one(runtime / 'tools/node', 'node.exe')
    ffmpeg = find_one(runtime / 'tools/ffmpeg', 'ffmpeg.exe')
    ffprobe = find_one(runtime / 'tools/ffmpeg', 'ffprobe.exe')
    # Torch also ships the same CUPTI DLL. Select the explicitly pinned NVIDIA
    # wheel rather than treating two legitimate copies as an ambiguous install.
    cupti = paths['separation'].parents[1] / 'Lib/site-packages/nvidia/cuda_cupti/bin/cupti64_2022.3.0.dll'
    if not cupti.is_file(): raise FileNotFoundError(f'Pinned NVIDIA CUPTI payload missing: {cupti}')
    sep_dlls = [runtime / 'tools/vclibs', paths['separation'].parent, paths['separation'].parents[1] / 'Lib/site-packages/torch/lib']
    paddle_base = paths['paddle'].parents[1] / 'Lib/site-packages'
    cudnn_bins = list((runtime / 'tools/cudnn').glob('*/bin'))
    ocr_dlls = [runtime / 'tools/vclibs'] + cudnn_bins + [paths['paddle'].parent, paddle_base / 'paddle/libs'] + list((paddle_base / 'nvidia').glob('*/bin'))
    checkpoint = runtime / 'models/separation/97d170e1-dbb4db15.th'
    inventory = environment_inventory(paths['separation'])
    ocr_inventory = environment_inventory(paths['paddle'])
    model_docs = {}
    for name in ['PP-OCRv5_mobile_det', 'PP-OCRv5_mobile_rec']:
        folder = runtime / 'models' / name
        model_docs[name] = {'files': {p.name: sha(p) for p in folder.iterdir() if p.is_file() and p.name != 'README.md'}}
    ocr_dll_hashes = {}
    for directory in ocr_dlls:
        for p in directory.glob('*.dll'): ocr_dll_hashes.setdefault(p.name, sha(p))
    sep_dll_hashes = {}
    for directory in sep_dlls:
        for p in directory.glob('*.dll'): sep_dll_hashes.setdefault(p.name, sha(p))
    lock = {'schema_version': 1, 'cupti_sha256': sha(cupti),
        'dependency_hash_scope': 'Fresh install from release-hashed archives; installed RECORD identity is additional provenance.',
        'separate': {'python_version': inventory['python'], 'dependencies': inventory['distributions'],
            'dll_sha256': sep_dll_hashes,
            'model': {'sha256': sha(checkpoint)}},
        'ocr': {'python_version': ocr_inventory['python'], 'dependencies': ocr_inventory['distributions'],
            'dll_sha256': ocr_dll_hashes, 'models': model_docs}}
    lock_path = runtime / 'locks/separation-engine.json'; atomic(lock_path, lock)
    env = {role + '_python': str(path) for role, path in paths.items()}
    env.update(node=str(node), ffmpeg=str(ffmpeg), ffprobe=str(ffprobe),
        source_adapter=str(ROOT / 'vendor/core/source_adapter'), font='C:/Windows/Fonts/arialbd.ttf',
        preview_font='C:/Windows/Fonts/arial.ttf', logo=str(ROOT / 'assets/logo.png'), wordmark=str(ROOT / 'assets/word.png'),
        capcut_project_root=str(Path.home() / 'AppData/Local/CapCut/User Data/Projects/com.lveditor.draft'),
        ocr_det_model=str(runtime / 'models/PP-OCRv5_mobile_det'), ocr_rec_model=str(runtime / 'models/PP-OCRv5_mobile_rec'),
        ocr_dll_directories=[str(p) for p in ocr_dlls if p.is_dir()],
        separation_checkpoint=str(checkpoint), separation_engine_lock=str(lock_path), cupti_library=str(cupti),
        separation_dll_directories=[str(p) for p in sep_dlls if p.is_dir()],
        gpu_lock=str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NguyenHuuThinhWorkflow/gpu-0.lock'),
        runtime_manifest=str(runtime / 'runtime-manifest.json'))
    cfg = {'schema': 2, 'profile': 'ava-sfx-native-1080p30', 'env': env, 'preset': str(ROOT / 'config/preset.json'), 'retries': 0}
    atomic(output, cfg)
    return cfg


def provision(args):
    if os.name != 'nt' or sys.maxsize <= 2**32 or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Setup requires Windows x64 with CPython 3.12; pass -PythonPath to setup.ps1')
    driver = nvidia_driver()
    print('NVIDIA driver probe: ' + json.dumps(driver), flush=True)
    runtime = args.runtime_root.resolve(); runtime.mkdir(parents=True, exist_ok=True)
    if runtime == ROOT or runtime.is_relative_to(ROOT):
        raise ValueError('Runtime root must be outside the source/ZIP directory')
    lock = load(ROOT / 'config/downloads.lock.json')
    with process_lock(runtime / 'setup.lock'):
        attempt = runtime / 'setup-attempts' / str(time.time_ns()); attempt.mkdir(parents=True)
        log = attempt / 'setup.log'
        atomic(attempt / 'begin.json', {'utc': utc(), 'runtime_root': str(runtime), 'status': 'RUNNING', 'nvidia_driver': driver})
        try:
            for key in ['node', 'ffmpeg', 'cudnn', 'vclibs']:
                artifact = download(lock['artifacts'][key], args.artifact_cache or runtime / 'downloads')
                folder = runtime / 'tools' / key
                if not folder.exists():
                    extract_zip(artifact, folder)
                verify_zip(artifact, folder)
            for item in lock['models']:
                cached = download(item, args.artifact_cache or runtime / 'downloads')
                target = runtime / item['target']; target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and sha(target) != item['sha256']:
                    raise ValueError(f'Model changed: {target}; select a new runtime root')
                if not target.exists(): shutil.copyfile(cached, target)
            paths = {}
            for role in ROLES:
                print(f'Provisioning {role}; log {log}', flush=True)
                paths[role] = install_role(role, runtime, Path(sys.executable), log, args.package_cache)
            # Adapter install is private to this extracted source tree; no global npm.
            node = find_one(runtime / 'tools/node', 'node.exe')
            npm = node.parent / 'node_modules/npm/bin/npm-cli.js'
            adapter = ROOT / 'vendor/core/source_adapter'
            run([node, npm, 'ci', '--ignore-scripts', '--prefix', adapter], log)
            cfg = prepare_config(runtime, paths, lock['artifacts'], args.config_output)
            files = {}
            for base in [runtime / 'envs', runtime / 'tools', runtime / 'models', runtime / 'locks']:
                for p in base.rglob('*'):
                    if p.is_file() and p.suffix != '.pyc' and '__pycache__' not in p.parts:
                        files[str(p)] = sha(p)
            adapter_modules = adapter / 'node_modules'
            files.update({str(p): sha(p) for p in adapter_modules.rglob('*') if p.is_file()})
            base_python = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
            files[str(base_python)] = sha(base_python)
            for pattern in ('python*.dll', 'vcruntime*.dll'):
                for path in Path(sys.base_prefix).glob(pattern):
                    if path.is_file(): files[str(path)] = sha(path)
            atomic(runtime / 'runtime-manifest.json', {'schema': 1, 'created_utc': utc(), 'files': files,
                'downloads_lock_sha256': sha(ROOT / 'config/downloads.lock.json'),
                'requirements': {role: sha(ROOT / 'config/locks' / f'{role}.requirements.txt') for role in ROLES},
                'scope': 'Every installed runtime/tool/model file except bytecode; no source film or user draft'})
            # Successful installation is not an engine acceptance claim.
            run([paths['controller'], '-B', ROOT / 'tools/doctor.py', '--config', args.config_output,
                 '--output', attempt / 'doctor'], log, timeout=1800)
            run([paths['controller'], '-B', ROOT / 'tools/smoke.py', '--config', args.config_output,
                 '--output', attempt / 'smoke', '--online'], log, timeout=7200)
            atomic(attempt / 'result.json', {'status': 'PASS', 'config': str(args.config_output), 'ended_utc': utc()})
            print(f'Setup and real GPU/online smoke passed. Config: {args.config_output}', flush=True)
        except BaseException as exc:
            atomic(attempt / 'result.json', {'status': 'FAILED', 'error_type': type(exc).__name__, 'error': str(exc), 'ended_utc': utc()})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--config-output', type=Path, required=True)
    parser.add_argument('--artifact-cache', type=Path, help='Optional shared download cache; every byte is still hash checked')
    parser.add_argument('--package-cache', type=Path, help='Optional pip cache; --require-hashes remains mandatory')
    args = parser.parse_args()
    provision(args)


if __name__ == '__main__': main()
