from pathlib import Path
from fractions import Fraction
import json, subprocess
from .common import command, atomic, sha, VENDOR

def validate_profile(video):
    if video.get('codec_name')!='hevc' or (video.get('width'),video.get('height'))!=(1920,1080):
        raise ValueError('Supported source profile requires native 1920x1080 HEVC')
    fps=Fraction(video['avg_frame_rate'])
    if fps!=30 or Fraction(video['r_frame_rate'])!=fps:
        raise ValueError('Supported source profile requires CFR30')
    if video.get('sample_aspect_ratio','1:1') not in ['1:1','N/A']:
        raise ValueError('Supported source profile requires native square pixels')
    if video.get('pix_fmt')!='yuv420p' or video.get('color_transfer') in ['smpte2084','arib-std-b67']:
        raise ValueError('Supported source profile requires SDR yuv420p')
    return str(fps)


def probe(path, env):
    p=subprocess.run([env['ffprobe'],'-v','error','-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,check=True)
    d=json.loads(p.stdout);v=next(x for x in d['streams'] if x['codec_type']=='video');a=next(x for x in d['streams'] if x['codec_type']=='audio')
    frames=int(v.get('nb_frames',0))
    if frames<=0:raise ValueError('Source requires exact nb_frames; no duration-based guessing')
    fps=validate_profile(v)
    return dict(path=str(Path(path).resolve()),sha256=sha(path),frames=frames,fps=fps,width=v['width'],height=v['height'],sample_rate=48000,video_stream=v,audio_stream=a,seconds=float(Fraction(frames,1)/Fraction(fps)))

def validate(row,out,env):
    path=Path(row['path']).resolve(strict=True);expected=row.get('sha256')
    if not expected or sha(path)!=expected:raise ValueError('Source SHA256 absent or mismatched; preserve original and update job explicitly')
    source=probe(path,env)
    inherited=row.get('adapter_validation')
    if inherited:
        report=Path(inherited['report'])
        if sha(report)!=inherited['report_sha256']:raise ValueError('Download validation report changed')
        doc=json.loads(report.read_text(encoding='utf8'));matches=[x for x in doc['episodes'] if x['episode']==row['episode']]
        if len(matches)!=1:raise ValueError('Missing exact downloaded episode validation')
        evidence=matches[0]['validation']
        if matches[0]['status']!='ready' or evidence['fullDecodeExitCode']!=0 or evidence['sha256']!=source['sha256'] or evidence['frameCount']!=source['frames']:raise ValueError('Source adapter decode/hash/scope mismatch')
        source['decode_reused_from_verified_download']=inherited
    else:
        command([env['ffmpeg'],'-v','error','-xerror','-nostdin','-i',path,'-f','null','-'],out,name='source-full-decode')
    source.update(episode=row['episode'],fresh_download=bool(row.get('fresh_download',False)),verification='Full decode once, either matched successful same-job source-adapter receipt or this source stage; later cache uses content hashes')
    # The live MP4 belongs to the cache boundary, not only its metadata JSON.
    atomic(out/'source.json',source);source['files']=[str(out/'source.json'),str(path)];return source

def download(job,out,env):
    adapter=Path(env.get('source_adapter',VENDOR/'source_adapter'))
    cfg={'seriesId':str(job['series_id']),'episodes':job['episodes'],'outputRoot':str(out/'download'),'concurrency':1,'ffmpeg':env['ffmpeg'],'ffprobe':env['ffprobe']}
    atomic(out/'download-config.json',cfg)
    command([env['node'],adapter/'cli.cjs','download','--config',out/'download-config.json'],out,name='download',timeout=7200)
    report=out/'download/download_report.json'
    d=json.loads(report.read_text(encoding='utf8'))
    if not all(e.get('status')=='ready' and not e.get('reusedExisting') for e in d['episodes']):raise ValueError('Fresh download did not create every requested episode')
    rows=[{'episode':n,'path':str(out/'download/sources'/f'ep{n:02}_app_1080p.mp4'),'sha256':sha(out/'download/sources'/f'ep{n:02}_app_1080p.mp4'),'fresh_download':True,'adapter_validation':{'report':str(report),'report_sha256':sha(report)}} for n in job['episodes']]
    return {'sources':rows,'files':[str(p) for p in out.rglob('*') if p.is_file()]}
