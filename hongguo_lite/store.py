from __future__ import annotations
import time, traceback
from pathlib import Path
from .common import atomic, load, sha, digest, utc, event

class Blocked(RuntimeError): pass

class Store:
    def __init__(self, root, reuse_from=None):
        self.root=Path(root).resolve(); self.state_path=self.root/'state.json'
        self.state=load(self.state_path) if self.state_path.exists() else {'schema':1,'stages':{}}
        self.stats={'executed':0,'reused':0,'adopted':0}
        self.reuse_from=Path(reuse_from).resolve() if reuse_from else None
        if self.reuse_from==self.root:raise ValueError('reuse_from must reference a different, preserved run')

    def save(self): atomic(self.state_path,self.state)

    def cached(self,key,inputs):
        old=self.state['stages'].get(key,{})
        if old.get('status')!='COMPLETE' or old.get('signature')!=digest(inputs):return None
        receipt=Path(old['receipt'])
        if not receipt.is_file() or sha(receipt)!=old.get('receipt_sha256'):return None
        r=load(receipt)
        if all(Path(p['path']).is_file() and sha(p['path'])==p['sha256'] for p in r['outputs']):return r['result']
        return None

    def stage(self, key, inputs, action, *, retries=0):
        signature=digest(inputs); old=self.state['stages'].get(key)
        cached=self.cached(key,inputs)
        if cached is not None:
            self.stats['reused']+=1;event(self.root,'cache_reuse',stage=key,signature=signature)
            return cached
        adopted=None
        if self.reuse_from:
            previous=Store(self.reuse_from);candidate=previous.cached(key,inputs)
            if candidate is not None:
                original=previous.state['stages'][key]
                adopted={**candidate,'reuse_provenance':{'run':str(self.reuse_from),'receipt':original['receipt'],'receipt_sha256':original['receipt_sha256'],'policy':'Explicit run reuse; exact relevant input/config/engine signature and every artifact hash verified. Original receipt remains immutable.'}}
                event(self.root,'adopt_verified_stage',stage=key,original_receipt=original['receipt'],signature=signature)
        parent=self.root/'artifacts'/key; parent.mkdir(parents=True,exist_ok=True)
        for retry in range(retries+1):
            n=1
            while (parent/f'attempt-{n:04}').exists():n+=1
            out=parent/f'attempt-{n:04}';out.mkdir()
            start=time.monotonic();event(self.root,'begin',stage=key,attempt=n,signature=signature)
            self.state['stages'][key]={'status':'RUNNING','signature':signature,'attempt':str(out),'begin_utc':utc()};self.save()
            try:
                result=adopted if adopted is not None else action(out)
                # No success is committed before result and every declared file exist.
                atomic(out/'result.json',result)
                files={Path(p).resolve() for p in result.get('files',[])}|{(out/'result.json').resolve()}
                if any(not p.is_file() or (p.stat().st_size==0 and p.suffix.lower() in ['.wav','.mp4','.png','.jpg','.json','.srt']) for p in files):raise ValueError('Missing/empty declared stage artifact')
                outputs=[{'path':str(p),'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(files)]
                receipt=out/'receipt.json';atomic(receipt,{'schema':1,'stage':key,'signature':signature,'inputs':inputs,'result':result,'outputs':outputs,'seconds':time.monotonic()-start,'end_utc':utc()})
                self.state['stages'][key]={'status':'COMPLETE','signature':signature,'receipt':str(receipt),'receipt_sha256':sha(receipt),'attempt':str(out)};self.save()
                self.stats['adopted' if adopted is not None else 'executed']+=1;event(self.root,'end',stage=key,attempt=n,seconds=time.monotonic()-start,status='COMPLETE',adopted=adopted is not None);return result
            except Exception as exc:
                failure={'status':'BLOCKED' if isinstance(exc,Blocked) else 'FAILED','stage':key,'signature':signature,'attempt':str(out),'error':str(exc),'error_type':type(exc).__name__,'traceback':traceback.format_exc(),'seconds':time.monotonic()-start,'resume':'Correct the cited input/configuration and run resume. Failed attempt is preserved.'}
                atomic(out/'failure.json',failure);self.state['stages'][key]=failure;self.save();event(self.root,'end',stage=key,attempt=n,seconds=failure['seconds'],status=failure['status'],error=str(exc))
                if isinstance(exc,Blocked) or retry==retries:raise

    def result(self,key):
        item=self.state['stages'].get(key,{})
        if item.get('status')!='COMPLETE':raise Blocked(f'{key} is not complete; run/resume first')
        receipt=Path(item['receipt'])
        if sha(receipt)!=item['receipt_sha256']:raise ValueError(f'{key}: receipt hash changed')
        r=load(receipt)
        if not all(Path(p['path']).is_file() and sha(p['path'])==p['sha256'] for p in r['outputs']):raise ValueError(f'{key}: artifact hash changed; resume to rebuild')
        return r['result']
