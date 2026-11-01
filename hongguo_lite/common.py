from __future__ import annotations
import contextlib, datetime, hashlib, json, os, subprocess, sys, time, uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
VENDOR = PROJECT / 'vendor/core'
sys.path.insert(0, str(VENDOR / 'src'))

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def atomic(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf8', newline='\n') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

def event(root, kind, **values):
    path = Path(root) / 'execution.jsonl'; path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf8') as f:
        f.write(json.dumps(dict(utc=utc(), kind=kind, **values), ensure_ascii=False) + '\n')

def command(argv, out, *, timeout=7200, cwd=None, name='process'):
    out = Path(out); out.mkdir(parents=True, exist_ok=True); tick=time.monotonic()
    argv = list(map(str, argv)); atomic(out/(name+'-command.json'), {'argv':argv,'begin_utc':utc()})
    with (out/(name+'.stdout.log')).open('w', encoding='utf8') as so, (out/(name+'.stderr.log')).open('w', encoding='utf8') as se:
        proc = subprocess.run(argv, stdout=so, stderr=se, cwd=cwd, timeout=timeout,
                              env={**os.environ, 'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'})
    atomic(out/(name+'-result.json'), {'exit_code':proc.returncode,'seconds':time.monotonic()-tick,'end_utc':utc()})
    if proc.returncode:
        raise RuntimeError(f'{name} exited {proc.returncode}; see {out/(name+".stderr.log")}')

@contextlib.contextmanager
def process_lock(path):
    """OS advisory lock: stale filename never means an active process."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        if f.seek(0,2)==0: f.write(b'0');f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e: raise RuntimeError(f'Resource in use: {path}; resume after its holder finishes') from e
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)

def code_hash(*modules):
    files=[]
    for m in modules:
        p=PROJECT/m
        files.extend(sorted(x for x in p.rglob('*') if x.is_file() and x.suffix in ['.py','.js','.cjs','.json'] and not any(part in ['node_modules','__pycache__','.git'] for part in x.parts)) if p.is_dir() else [p])
    return digest({str(p.relative_to(PROJECT)):sha(p) for p in files})

def expand(value, variables):
    if isinstance(value, dict): return {k:expand(v,variables) for k,v in value.items()}
    if isinstance(value, list): return [expand(v,variables) for v in value]
    if isinstance(value, str):
        for k,v in variables.items(): value=value.replace('${'+k+'}',str(v))
    return value

def config(path):
    path=Path(path).resolve(); c=load(path)
    variables={**os.environ, 'PROJECT':PROJECT, 'CONFIG':path.parent, **c.get('variables',{})}
    c=expand(c, variables)
    def resolve_path(value):
        if isinstance(value,list):return [resolve_path(v) for v in value]
        if not isinstance(value,str):return value
        if '${' in value:raise ValueError('Unresolved configuration variable: '+value)
        return str(Path(value) if Path(value).is_absolute() else (path.parent/value).resolve())
    for k,v in c['env'].items():
        if k not in ['asr_backend','_asr_backend']:c['env'][k]=resolve_path(v)
    for k in ['preset']:
        if k in c:c[k]=resolve_path(c[k])
    c['_path']=str(path)
    return c
