"""Publish a source ZIP only when measured acceptance binds its exact payload."""
import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hongguo_lite.common import atomic,digest,load,sha,utc

INCLUDE=('hongguo_lite','tools','vendor','assets','licenses','config','tests','docs',
         'README.md','CHANGELOG.md','NOTICE.md','FORK-PROVENANCE.json','workflow.ps1','setup.ps1')
EXCLUDE={'node_modules','__pycache__','.git','runtime','runs','deliveries','executions','local-locks'}
CHECKS=('unit_tests','source_adapter_tests','clean_setup','relocation','engine_smoke','editorial_integration','original_preservation')


def source_files(root):
    root=Path(root).resolve();result={}
    for entry in INCLUDE:
        p=root/entry
        if not p.exists():raise FileNotFoundError(f'Required distribution content absent: {entry}')
        candidates=p.rglob('*') if p.is_dir() else [p]
        for item in candidates:
            rel=item.relative_to(root)
            if not item.is_file() or any(part in EXCLUDE for part in rel.parts):continue
            if item.name=='local.json' or item.suffix in ['.pyc','.log','.mp4','.mp3','.wav','.srt','.onnx','.pdiparams','.th','.pth','.dll','.exe']:continue
            if item.is_symlink():raise ValueError('Distribution cannot follow symlinks: '+str(rel))
            result[rel.as_posix()]=sha(item)
    return dict(sorted(result.items()))


def source_digest(root):return digest(source_files(root))


def package(root,archive,evidence,version):
    root=Path(root).resolve(strict=True);archive=Path(archive).resolve();evidence=Path(evidence).resolve(strict=True)
    if archive.exists() or archive.with_suffix(archive.suffix+'.receipt.json').exists():raise FileExistsError('Versioned release exists; preserve it')
    # Reject overlaps before creating any output, including self-copy recursion.
    for entry in INCLUDE:
        included=root/entry
        if archive.is_relative_to(included) or included.is_relative_to(archive):raise ValueError('Release output overlaps copied source tree')
    if archive.is_relative_to(evidence):raise ValueError('Release output overlaps evidence input')
    files=source_files(root);identity=digest(files);acceptance=load(evidence/'acceptance.json')
    if acceptance.get('source_digest')!=identity or acceptance.get('version')!=version:raise ValueError('Acceptance does not bind the final source/version')
    if acceptance.get('status')!='PASS' or set(acceptance.get('checks',{}))!=set(CHECKS) or any(acceptance['checks'][key].get('status')!='PASS' for key in CHECKS):raise ValueError('All required measured acceptance checks must pass')
    for key in CHECKS:
        proof=acceptance['checks'][key].get('evidence',{})
        if not proof.get('path') or not proof.get('sha256'):raise ValueError('Measured check needs hash-bound evidence: '+key)
        path=(evidence/proof['path']).resolve()
        if not path.is_relative_to(evidence) or not path.is_file() or sha(path)!=proof['sha256']:raise ValueError('Acceptance evidence missing, changed or outside evidence directory: '+key)
    archive.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workflow-release-',dir=archive.parent) as temp:
        staging=Path(temp)/'payload';staging.mkdir()
        for rel,wanted in files.items():
            out=staging/rel;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(root/rel,out)
            if sha(out)!=wanted:raise ValueError('Source changed during packaging: '+rel)
        for p in evidence.iterdir():
            if p.is_file() and p.suffix in ['.json','.md']:
                out=staging/'validation'/p.name;out.parent.mkdir(exist_ok=True);shutil.copy2(p,out)
        atomic(staging/'release.json',dict(version=version,software_status='STABLE',source_digest=identity,built_utc=utc(),
            profile='Windows x64 / NVIDIA GPU FP32 / Hongguo 1920x1080 HEVC CFR30 / manual CapCut Chinese / Ava + SFX -5dB',
            portable=False,setup_downloads_runtime=True,video_acceptance='Each film remains DRAFT until actual review',external_published=False))
        entries={p.relative_to(staging).as_posix():{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(staging.rglob('*')) if p.is_file()}
        atomic(staging/'manifest.sha256.json',{'algorithm':'sha256','files':entries,'excludes_itself':True})
        candidate=Path(temp)/archive.name
        with zipfile.ZipFile(candidate,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for p in sorted(staging.rglob('*')):
                if p.is_file():z.write(p,p.relative_to(staging).as_posix())
        with zipfile.ZipFile(candidate) as z:
            if z.testzip() is not None:raise ValueError('ZIP CRC failed')
            for rel,record in entries.items():
                if hashlib.sha256(z.read(rel)).hexdigest()!=record['sha256']:raise ValueError('ZIP payload hash failed: '+rel)
            extracted=Path(temp)/'verify extraction with spaces';z.extractall(extracted)
        for rel,record in entries.items():
            if sha(extracted/rel)!=record['sha256']:raise ValueError('Extracted hash failed: '+rel)
        if source_files(root)!=files:raise ValueError('Final sources changed while ZIP was being verified')
        candidate.rename(archive)
    receipt={'zip':archive.name,'sha256':sha(archive),'bytes':archive.stat().st_size,'files':len(entries)+1,
             'software_status':'STABLE','source_digest':identity,'crc':'PASS','payload_hashes':'PASS','extracted_hashes':'PASS','built_utc':utc()}
    atomic(archive.with_suffix(archive.suffix+'.receipt.json'),receipt)
    archive.with_suffix(archive.suffix+'.sha256').write_text(receipt['sha256']+'  '+archive.name+'\n',encoding='ascii')
    return receipt


if __name__=='__main__':
    p=argparse.ArgumentParser(__doc__);p.add_argument('--source',type=Path,default=ROOT);p.add_argument('--output',type=Path);p.add_argument('--evidence',type=Path);p.add_argument('--version',default='1.1.0');p.add_argument('--digest',action='store_true');a=p.parse_args()
    if a.digest:print(source_digest(a.source))
    else:
        if not a.output or not a.evidence:p.error('--output and --evidence required')
        print(json.dumps(package(a.source,a.output,a.evidence,a.version),indent=2))
