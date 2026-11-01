from __future__ import annotations
from pathlib import Path
from fractions import Fraction
import wave
from .common import command, atomic, sha, VENDOR
from .store import Blocked
from hongguo_ssot.fresh_media import pcm_info, tempo_filter, samples_at_frame
from hongguo_ssot.fresh_tts import cache_identity, _config

TEMPOS=[1.25+i*.25 for i in range(11)]

class PartialSynthesisError(RuntimeError):
    def __init__(self, message, results):
        super().__init__(message);self.results=results

def _verified_receipt(path,request):
    from .common import load
    r=load(path)
    if not isinstance(r,dict) or not isinstance(r.get('cue'),dict):raise ValueError('Malformed cue receipt')
    cue=next((c for c in request['cues'] if c['cue_id']==r.get('cue',{}).get('cue_id')),None)
    if cue is None or r.get('cue')!=cue or r.get('identity')!=cache_identity(cue,_config(request['engine_config']),request['model_lock']) or r.get('technical_status')!='PASS' or r.get('run_root')!=str(Path(request['output_dir']).resolve()):raise ValueError('Incomplete/stale worker cue receipt')
    required={'native-24000.wav','audio-48000.wav','engine-input.json','engine-result.json','resample-command.json'}
    if set(r.get('artifacts',{}))!=required:raise ValueError('Incomplete native synthesis metadata')
    base=Path(path).parent
    for name,h in r['artifacts'].items():
        if Path(name).name!=name or sha(base/name)!=h:raise ValueError('Corrupt completed cue artifact')
    if Path(r['audio_path']).resolve()!=base.resolve()/'audio-48000.wav' or r['audio_sha256']!=r['artifacts']['audio-48000.wav']:raise ValueError('Audio receipt path/hash mismatch')
    pcm_info(r['audio_path'],channels=1)
    return {'audio':r['audio_path'],'audio_sha256':r['audio_sha256'],'receipt':str(path),'files':[str(base/n) for n in required]+[str(path)],'native_speed':1,'sample_rate':48000,'canonical_text':cue['canonical_text']}

def recover_worker(out):
    """Only complete atomic cue receipts from this exact worker request."""
    from .common import load
    try:
        request=load(Path(out)/'request.json')
        if not isinstance(request,dict) or not isinstance(request.get('cues'),list):return {}
    except (ValueError,OSError):return {}
    results={}
    for p in (Path(out)/'audio').glob('cue-*/receipt.json'):
        try:r=_verified_receipt(p,request)
        except (ValueError,OSError,KeyError,TypeError,AttributeError,wave.Error,EOFError):continue
        cue=load(p)['cue'];results[cue['cue_id']]=r
    return results

def synthesize(cues,out,env,model_lock,run_id):
    rules=VENDOR/'config/pronunciation-rules-v1.json'
    request={'engine_config':{'voice':'af_heart','language':'a','device':'cpu','precision':'float32','speed':1,'sample_rate':48000,'channels':1,'pcm_subtype':'PCM_16','seed':0,'num_threads':4,'timeout_seconds':120,'run_id':run_id,'ffmpeg':env['ffmpeg'],'pronunciation_rules_path':str(rules),'pronunciation_rules_sha256':sha(rules)},'model_lock':model_lock,'output_dir':str(out/'audio'),'runtime_cache_dir':str(out/'cache'),'resume':False,'dll_directories':env.get('tts_dll_directories',[]),'cues':cues}
    atomic(out/'request.json',request)
    try:command([env['tts_python'],VENDOR/'tools/fresh_tts_worker.py','--request',out/'request.json','--response',out/'response.json'],out,name='kokoro',timeout=7200)
    except Exception as exc:raise PartialSynthesisError(str(exc),recover_worker(out)) from exc
    from .common import load
    response=load(out/'response.json')
    if response.get('technical_status')!='PASS':raise ValueError(f'Kokoro failed: {out/"response.json"}')
    result={}
    for r in response['receipts']:
        result[r['cue']['cue_id']]=_verified_receipt(r['receipt_path'],request)
    return result

def fit(cue,tts,next_start,total_frames,fps,out,env):
    start=samples_at_frame(cue['start_frame'],fps);boundary=samples_at_frame(next_start,fps);window=boundary-start
    raw=Path(tts['audio']);original=pcm_info(raw,channels=1)
    row={'cue_id':cue['cue_id'],'canonical_text':cue['canonical_text'],'start_frame':cue['start_frame'],'end_frame_exclusive':cue['end_frame_exclusive'],'start_sample':start,'available_samples':window,'raw_audio':str(raw),'raw_sha256':sha(raw),'raw_samples':original['samples'],'tempo_attempts':[],'status':'NEEDS_EDIT'}
    for t in TEMPOS:
        output=out/f'tempo-{int(t*100):03}.wav'
        command([env['ffmpeg'],'-v','error','-nostdin','-n','-i',raw,'-vn','-af',tempo_filter(t),'-c:a','pcm_s16le','-ar','48000','-ac','1',output],out,name=f'tempo-{int(t*100):03}')
        actual=pcm_info(output,channels=1)['samples'];attempt={'tempo':t,'samples':actual,'path':str(output),'sha256':sha(output),'fits':actual<=window};row['tempo_attempts'].append(attempt)
        if actual<=window:
            row.update(status='PASS',tempo=t,samples=actual,path=str(output),sha256=sha(output),end_sample=start+actual);break
    atomic(out/'fit.json',row)
    # A failed fit is a valid measured result, never success for downstream audio.
    row['files']=[str(out/'fit.json')]+[r['path'] for r in row['tempo_attempts']]
    return row

def timeline(rows,source,out):
    if any(r['status']!='PASS' for r in rows):raise Blocked('NEEDS_EDIT: one or more measured cues exceed 3.75; export translation, edit with evidence, import, resume')
    total=samples_at_frame(source['frames'],source['fps']);cursor=0;path=out/'voice48.wav'
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,48000,0,'NONE','not compressed'))
        for r in rows:
            if r['start_sample']<cursor:raise ValueError('Speech overlap forbidden')
            gap=r['start_sample']-cursor
            while gap:
                n=min(gap,48000);w.writeframesraw(bytes(n*2));gap-=n
            with wave.open(r['path'],'rb') as a:
                while block:=a.readframes(48000*8):w.writeframesraw(block)
            cursor=r['end_sample']
        gap=total-cursor
        if gap<0:raise ValueError('Speech exceeds native video')
        while gap:
            n=min(gap,48000);w.writeframesraw(bytes(n*2));gap-=n
    pcm_info(path,channels=1,samples=total)
    report={'path':str(path),'sha256':sha(path),'samples':total,'priority_listening':[{'cue_id':r['cue_id'],'tempo':r['tempo'],'text':r['canonical_text']} for r in rows if r['tempo']>=2.5],'files':[str(path)],'listening':'NOT_PERFORMED'}
    atomic(out/'voice.json',report);report['files'].append(str(out/'voice.json'));return report
