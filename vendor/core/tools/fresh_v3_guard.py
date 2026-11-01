"""Hold a shared OS GPU lock inside the actual model process, even on parent death."""
import argparse
from pathlib import Path
import runpy
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from hongguo_ssot.storage import process_lock

if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock",type=Path,required=True)
    p.add_argument("--wait-seconds",type=float,default=7200)
    p.add_argument("script",type=Path)
    p.add_argument("args",nargs=argparse.REMAINDER)
    a=p.parse_args()
    script=a.script.resolve(strict=True)
    sys.path.insert(0,str(script.parent))
    sys.argv=[str(script),*a.args]
    deadline=time.monotonic()+a.wait_seconds
    while True:
        lock=process_lock(a.lock)
        try:
            lock.__enter__()
            break
        except RuntimeError:
            if time.monotonic()>=deadline: raise
            time.sleep(.2)
    try: runpy.run_path(str(script),run_name="__main__")
    finally: lock.__exit__(None,None,None)
