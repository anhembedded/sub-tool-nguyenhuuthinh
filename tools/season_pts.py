"""Exact MP4 video-sample PTS/duration QA alongside prior full-decode receipts."""
import argparse
import json
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,utc
from hongguo_lite.season_contracts import unsealed


def main(root):
    unsealed(root)
    m=load(root/'manifest.json');rows=load(root/'download/download_report.json')['episodes'];out=root/'native-pts-qa-v002';out.mkdir(exist_ok=True);tick=time.monotonic()
    def one(row):
        path=out/f"ep{row['episode']:03d}.json"
        data=json.loads(subprocess.check_output([m['environment']['ffprobe'],'-v','error','-select_streams','v:0','-show_packets','-show_entries','packet=pts,duration','-of','json',row['path']],text=True))
        stream=next(s for s in row['validation']['probe']['streams'] if s['codec_type']=='video');step=1/Fraction(row['validation']['frameRate'])/Fraction(stream['time_base']);packets=sorted(data['packets'],key=lambda p:int(p['pts']));errors=[]
        for i,p in enumerate(packets):
            if Fraction(p['pts'])!=i*step or Fraction(p.get('duration',0))!=step:errors.append(dict(sample=i,packet=p,expected_pts=str(i*step),expected_duration=str(step)))
        valid=len(packets)==row['validation']['frameCount'] and not errors and row['validation']['fullDecodeExitCode']==0 and sha(row['path'])==row['validation']['sha256']
        result=dict(episode=row['episode'],status='PASS' if valid else 'FAIL',source_sha256=row['validation']['sha256'],video_samples=len(packets),metadata_frames=row['validation']['frameCount'],time_base=stream['time_base'],frame_step_ticks=str(step),errors=errors,full_decode_exit_code=row['validation']['fullDecodeExitCode'],method='MP4 video sample PTS/duration sorted in presentation order; prior full decode is independently hash-bound. No claim that packet inspection itself decoded frames.')
        atomic(path,result);print(f"Native sample PTS EP{row['episode']:03d} {result['status']}",flush=True);return result
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(one,rows))
    atomic(out/'report.json',dict(status='PASS' if all(r['status']=='PASS' for r in results) else 'FAIL',episodes=results,seconds=time.monotonic()-tick,completed_utc=utc(),worker_sha256=sha(__file__)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args();main(a.run.resolve())
