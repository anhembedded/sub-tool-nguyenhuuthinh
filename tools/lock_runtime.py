"""Maintainer tool: resolve release requirements once, retaining archive hashes.

Not called by setup. A release ships the resulting complete locks; setup cannot
resolve, upgrade, or relax them. Run under Windows CPython 3.12 x64.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hongguo_lite.common import atomic

TOP = {
 'controller': ['numpy==1.26.4', 'Pillow==13.0.0', 'srt==3.5.2'],
 'edge': ['edge-tts==7.3.1'],
 'paddle': ['paddleocr==3.4.0', 'paddlex==3.4.3', 'numpy==2.2.6', 'Pillow==13.0.0', 'paddlepaddle-gpu @ https://paddle-whl.cdn.bcebos.com/stable/cu118/paddlepaddle-gpu/paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl'],
 'separation': ['demucs==4.0.1', 'torch==2.5.1+cu118', 'torchaudio==2.5.1+cu118', 'numpy==2.5.4', 'scipy==1.18.1', 'soundfile==0.14.0', 'nvidia-cuda-cupti-cu11==11.8.87'],
 'capcut': ['pycapcut @ https://codeload.github.com/GuanYixuan/pyCapCut/zip/ec61408d9f039c4045f946370bb35746b89b2479', 'pymediainfo==7.0.1', 'imageio==2.38.1', 'Pillow==13.0.0'],
}


def get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'nguyenhuuthinh-release-lock/1.1.0'})
    return urllib.request.urlopen(request, timeout=120).read()


def role_lock(role, work):
    report = work / (role + '-resolve.json'); log = work / (role + '-resolve.log')
    resolver = work / 'bootstrap/Scripts/python.exe'
    command = [str(resolver), '-m', 'pip', 'install', '--dry-run', '--ignore-installed', '--no-build-isolation',
        '--index-url', 'https://pypi.org/simple', '--report', str(report), '--cache-dir', str(work / 'pip-cache')]
    if role == 'separation': command += ['--extra-index-url', 'https://download.pytorch.org/whl/cu118']
    with log.open('wb') as output:
        subprocess.run(command + TOP[role], stdout=output, stderr=output, check=True, timeout=7200)
    doc = json.loads(report.read_text())
    lines = ['# Windows x64 CPython 3.12; full resolver closure; setup installs with --no-deps --require-hashes.', '# Sources and archive hashes are captured from the primary package indexes.']
    packages = []
    for row in doc['install']:
        info = row['download_info']; digest = info['archive_info']['hashes']['sha256']
        name = row['metadata']['name']; url = info['url']
        if not url.startswith('https://'): raise ValueError('Non-HTTPS package source')
        packages.append((name.lower(), f'{name} @ {url} --hash=sha256:{digest}'))
    lines += [line for _, line in sorted(packages)]
    (ROOT / 'config/locks' / f'{role}.requirements.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    atomic(ROOT / 'config/locks' / f'{role}.archives.json', {'roots': TOP[role], 'packages': [
        {'name': r['metadata']['name'], 'version': r['metadata']['version'], 'url': r['download_info']['url'], 'sha256': r['download_info']['archive_info']['hashes']['sha256']}
        for r in doc['install']]})
    return role, len(packages)


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--work', type=Path, required=True)
    p.add_argument('--roles', nargs='+', choices=list(TOP), default=list(TOP)); args = p.parse_args()
    args.work.mkdir(parents=True, exist_ok=True); (ROOT / 'config/locks').mkdir(exist_ok=True)
    build = []
    for name, version in [('pip', '25.0.1'), ('setuptools', '78.1.0'), ('wheel', '0.45.1')]:
        doc = json.loads(get(f'https://pypi.org/pypi/{name}/{version}/json'))
        item = next(x for x in doc['urls'] if x['filename'].endswith('py3-none-any.whl'))
        build.append(f"{name} @ {item['url']} --hash=sha256:{item['digests']['sha256']}")
    (ROOT / 'config/locks/build.requirements.txt').write_text('\n'.join(build) + '\n')
    resolver = args.work / 'bootstrap/Scripts/python.exe'
    if not resolver.exists(): subprocess.run([sys.executable, '-m', 'venv', str(resolver.parents[1])], check=True)
    subprocess.run([str(resolver), '-m', 'pip', 'install', '--require-hashes', '--no-deps', '-r', str(ROOT / 'config/locks/build.requirements.txt')], check=True)
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        futures = {pool.submit(role_lock, role, args.work): role for role in args.roles}
        for future in concurrent.futures.as_completed(futures): print(future.result(), flush=True)


if __name__ == '__main__': main()
