"""pyvideotrans-style audio-only autorate, measured in PCM samples.

Reference: videotrans/task/_rate.py::_calculate_adjustments and
_precise_speed_up_audio. Keep short speech at the configured minimum, and
speed up longer speech by raw/window. Preserve the configured maximum and
no-cut/no-video-slowdown rules.
Unlike upstream's -t, overlong filter output is measured and retried, not cut.
"""
from pathlib import Path
from array import array
import wave
import time
from .common import atomic, command, sha
from .audio_clock import pcm_info, samples_at_frame

POLICY='pyvideotrans-audio-only-ratio-v1'

def trim_outer_silence(raw,output,threshold=32,margin_samples=3840):
    """Remove only outer samples below -60 dBFS, retaining 80 ms margins.

    Original waveform is preserved. No interior silence or active sample is cut.
    """
    with wave.open(str(raw),'rb') as w:
        params=w.getparams()
        if (w.getnchannels(),w.getsampwidth(),w.getframerate())!=(1,2,48000):raise ValueError('Expected PCM48 mono16')
        data=array('h',w.readframes(w.getnframes()))
    active=[i for i,n in enumerate(data) if abs(n)>threshold]
    if not active:return raw,dict(applied=False,reason='No active speech above threshold')
    left=max(0,active[0]-margin_samples);right=min(len(data),active[-1]+1+margin_samples)
    if left==0 and right==len(data):return raw,dict(applied=False,reason='No removable outer silence')
    assert all(abs(n)<=threshold for n in data[:left]) and all(abs(n)<=threshold for n in data[right:])
    with wave.open(str(output),'wb') as w:w.setparams(params);w.writeframes(data[left:right].tobytes())
    return output,dict(applied=True,threshold_pcm16=threshold,retained_margin_samples=margin_samples,removed_leading_samples=left,removed_trailing_samples=len(data)-right,output=str(output),sha256=sha(output))

def tempo_filter(factor):
    if not 1.0<=factor<=50: raise ValueError('Invalid tempo')
    chain=[]
    while factor>2:
        chain.append('atempo=2.0');factor/=2
    chain.append(f'atempo={factor:.12g}')
    return ','.join(chain)

def fit(cue,tts,next_start,total_frames,fps,out,env,max_tempo=3.75,min_tempo=1.0):
    if not 1<=min_tempo<=max_tempo<=50:raise ValueError('Invalid tempo limits')
    destination=Path(out);destination.mkdir(parents=True,exist_ok=True)
    # A killed FFmpeg may leave a partial ratio WAV. Every retry gets its own
    # directory, while fit.json is committed only after measured completion.
    out=destination/f'attempt-{time.time_ns()}';out.mkdir()
    raw=Path(tts['audio']); info=pcm_info(raw,channels=1)
    start=samples_at_frame(cue['start_frame'],fps)
    window=samples_at_frame(next_start,fps)-start
    row=dict(cue_id=cue['cue_id'],canonical_text=cue['canonical_text'],start_frame=cue['start_frame'],end_frame_exclusive=cue['end_frame_exclusive'],start_sample=start,available_samples=window,raw_audio=str(raw),raw_sha256=sha(raw),raw_samples=info['samples'],tempo_attempts=[],policy=POLICY,max_tempo=max_tempo,status='NEEDS_EDIT')
    if window<=0:
        row['status']='NEEDS_EDIT_OVERLAPPING_SOURCE'
    else:
        if info['samples']/window>max_tempo:
            # Brief Edge utterances can contain ~1s of trailing silence. Only
            # remove low-level outer silence before declaring speech too long.
            active,trim=trim_outer_silence(raw,out/'outer-silence-trimmed.wav')
            row['outer_silence_trim']=trim
            if trim['applied']:
                raw=Path(active);info=pcm_info(raw,channels=1)
                row['active_samples']=info['samples'];row['active_audio']=str(raw)
        required=max(min_tempo,info['samples']/window)
        row['required_tempo']=required
        if required>max_tempo:
            row['reason']='Required tempo exceeds configured maximum; no truncation or video slowdown'
        elif required==1:
            row.update(status='PASS',tempo=1.0,path=str(raw),samples=info['samples'],sha256=sha(raw),end_sample=start+info['samples'])
        else:
            factor=required
            for attempt in range(6):
                output=out/f'ratio-{attempt}.wav'
                command([env['ffmpeg'],'-v','error','-nostdin','-n','-i',raw,'-vn','-af',tempo_filter(factor),'-ar','48000','-ac','1','-c:a','pcm_s16le',output],out,name=f'ratio-{attempt}')
                actual=pcm_info(output,channels=1)['samples']
                row['tempo_attempts'].append(dict(tempo=factor,samples=actual,path=str(output),sha256=sha(output),fits=actual<=window))
                if actual<=window:
                    row.update(status='PASS',tempo=factor,samples=actual,path=str(output),sha256=sha(output),end_sample=start+actual);break
                factor=factor*(actual/window)*1.01
                if factor>max_tempo:break
    row['files']=[str(out/'fit.json'),str(destination/'fit.json')]+[a['path'] for a in row['tempo_attempts']]
    if row.get('outer_silence_trim',{}).get('applied'):row['files'].append(row['active_audio'])
    atomic(out/'fit.json',row)
    atomic(destination/'fit.json',row)
    return row
