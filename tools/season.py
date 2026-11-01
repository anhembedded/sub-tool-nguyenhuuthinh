"""Supported, timed entry point for the fork's CapCut/Ava season workflow."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from hongguo_lite.common import load, atomic, sha, utc, config, process_lock
from hongguo_lite.store import Store


def runtime_lock(env):
    """Record configured roles, with no developer directory-name assumptions."""
    roles={};versions={}
    def add(path,role):
        path=Path(path).resolve(strict=True)
        if path.is_dir():
            for child in path.rglob('*'):
                if child.is_file() and '__pycache__' not in child.parts:add(child,role)
        else:roles.setdefault(str(path),set()).add(role)
    manifest_path=Path(env['runtime_manifest'])
    runtime=load(manifest_path)
    role_directories={
        'controller':[Path(env['controller_python']).resolve().parents[1]],
        'edge':[Path(env['edge_python']).resolve().parents[1]],
        'capcut':[Path(env['capcut_python']).resolve().parents[1]],
        'ocr':[Path(env['paddle_python']).resolve().parents[1],Path(env['ocr_det_model']).resolve(),Path(env['ocr_rec_model']).resolve(),*map(lambda p:Path(p).resolve(),env.get('ocr_dll_directories',[]))],
        'separation':[Path(env['separation_python']).resolve().parents[1],Path(env['separation_checkpoint']).resolve().parent,*map(lambda p:Path(p).resolve(),env.get('separation_dll_directories',[]))],
        'source':[Path(env['node']).resolve().parent,Path(env['source_adapter']).resolve()/'node_modules'],
        'media':[Path(env['ffmpeg']).resolve().parent],
    }
    for path,expected in runtime['files'].items():
        if sha(path)!=expected:raise ValueError('Provisioned runtime payload changed: '+path)
        resolved=Path(path).resolve()
        matched=[role for role,folders in role_directories.items() if any(resolved.is_relative_to(folder) for folder in folders)]
        for role in matched or ['controller']:add(resolved,role)
    add(manifest_path,'controller')
    for role,keys in {
        'media':['ffmpeg','ffprobe'], 'visual':['font','preview_font','logo','wordmark'],
        'source':['node'], 'ocr':['ocr_det_model','ocr_rec_model'],
        'separation':['separation_checkpoint','separation_engine_lock','cupti_library'],
    }.items():
        for key in keys:add(env[key],role)
    probe="import importlib.metadata as m,json; print(json.dumps([{'name':d.metadata.get('Name'),'version':d.version,'record':str(d._path/'RECORD')} for d in m.distributions()]))"
    for role,key in [('controller','controller_python'),('edge','edge_python'),('ocr','paddle_python'),('separation','separation_python'),('capcut','capcut_python')]:
        add(env[key],role)
        distributions=json.loads(subprocess.check_output([env[key],'-X','utf8','-c',probe],text=True,encoding='utf-8'))
        versions[role]=distributions
        for item in distributions:
            if Path(item['record']).is_file():add(item['record'],role)
        if role=='capcut':
            package=subprocess.check_output([env[key],'-c','import pycapcut,pathlib; print(pathlib.Path(pycapcut.__file__).parent)'],text=True).strip()
            add(package,role)
    adapter=Path(env.get('source_adapter',PROJECT/'vendor/core/source_adapter'))/'node_modules'
    if not adapter.is_dir():raise FileNotFoundError('Run setup before init: source-adapter dependencies missing')
    for item in adapter.rglob('*'):
        if item.is_file():add(item,'source')
    return dict(schema=2,created_utc=utc(),files={p:sha(p) for p in sorted(roles)},file_roles={p:sorted(v) for p,v in roles.items()},versions=versions,
                scope='Configured tools, assets, models, CapCut helper, Node dependencies and installed distribution RECORDs; separation runtime payloads are separately locked. OS/driver image is not bundled.')


def workflow_files():
    paths = list((PROJECT/'tools').glob('*.py')) + list((PROJECT/'tools').glob('*.cjs'))
    paths += list((PROJECT/'hongguo_lite').glob('*.py'))
    paths += [p for p in (PROJECT/'vendor/core').rglob('*') if p.is_file() and p.suffix in ['.py','.js','.cjs','.mjs','.json'] and not any(part in ['__pycache__','node_modules'] for part in p.parts)]
    paths += [PROJECT/'config/preset.json',PROJECT/'FORK-PROVENANCE.json',PROJECT/'workflow.ps1']
    return {str(p.resolve()):sha(p) for p in paths}


def runtime_files_for_stage(action, files, file_roles=None):
    if not file_roles:return files
    selected={'controller','media','visual'}
    if action in ['preflight','download']:selected.add('source')
    if action in ['prepare']:selected.add('capcut')
    if action in ['ocr','names-import']:selected.add('ocr')
    if action in ['background']:selected.add('separation')
    if action in ['speech']:selected.add('edge')
    return {p:h for p,h in files.items() if selected.intersection(file_roles.get(p,[]))}


def verified_credit(root):
    """Credit existing active artifacts once, only after validating their receipts."""
    roots={root/'media-store'};paths=set()
    for pointer in ['active-generation.json','active-video-generation.json']:
        if not (root/pointer).exists():continue
        info=load(root/pointer);folder=Path(info['run'])
        snapshot=folder/'dependency-snapshot.json'
        if not snapshot.is_file():continue
        # An editorial revision intentionally invalidates the previous generation.
        # Preserve it, but do not subtract any of its bytes from the new budget.
        if any(not Path(p).is_file() or sha(p)!=h for p,h in load(snapshot).items()):continue
        roots.add(folder)
        roots.update(folder.glob('episode-stores/ep*'))
        if info.get('episode_map'):roots.update(Path(p) for p in load(info['episode_map']).values())
    for folder in roots:
        store=Store(folder)
        for key,item in store.state['stages'].items():
            if item.get('status')!='COMPLETE':continue
            store.result(key)
            for output in load(item['receipt'])['outputs']:
                path=Path(output['path']).resolve()
                if path.is_relative_to(root):paths.add(path)
    report=root/'download/download_report.json'
    if report.exists():
        for row in load(report)['episodes']:
            if row.get('status')=='ready':
                path=Path(row['path']).resolve()
                if not path.is_relative_to(root) or sha(path)!=row['validation']['sha256']:raise ValueError('Cannot credit unverified source')
                paths.add(path)
    return sum(p.stat().st_size for p in paths)


def disk_budget(source_bytes, duration, largest_episode, free):
    """Conservative retained-artifact budget, not a claimed actual file size."""
    if not duration or duration <= 0:
        return dict(status='BLOCKED_UNKNOWN_DURATION', free_bytes=free)
    # Four video copies: per-episode, concat intermediate, muxed MP4, delivery.
    # voice/mix/concat PCM, native+fit attempts, and raster/name evidence reserve.
    rate = 4 * (6000000 + 192000) / 8 + 8 * 48000 * 2 * 4
    workflow = int(source_bytes + duration * rate + 3 * 1024**3)
    transfer = int(source_bytes + 4 * largest_episode + 2 * 1024**3)
    required = max(workflow, transfer)
    return dict(status='PASS' if free >= required else 'BLOCKED_DISK',
                free_bytes=free, required_free_bytes=required,
                shortfall_bytes=max(0, required-free), source_bytes=source_bytes,
                duration_seconds=duration, derivative_budget_bytes_per_second=rate,
                evidence_reserve_bytes=3*1024**3,
                assumptions='Planning estimate: 6Mbps H264, four retained video copies; eight stereo float PCM equivalents for stems, source/raw/backgrounds, voice, mix, concat and ordinary speech cache. An additional 3GiB reserve covers evidence and ordinary retries. Heavy revision/fit retry histories may require more; never delete prior attempts automatically.')


def check_disk(root):
    pointer = load(root/'source-preflight.json'); report = load(pointer['report'])
    manifest = load(root/'manifest.json')
    if report['status'] != 'PASS_METADATA_ONLY' or report['series_id'] != manifest['series_id'] or [r['episode'] for r in report['episodes']] != manifest['episodes']:
        raise ValueError('Whole-season source preflight has not passed')
    duration = report['duration_seconds']
    if (root/'download/download_report.json').exists():
        actual = load(root/'download/download_report.json')
        if actual['status'] == 'complete':
            duration = sum(r['validation']['frameCount']/30 for r in actual['episodes'])
    result = disk_budget(report['source_bytes'], duration,
                         max(r['bytes'] for r in report['episodes']),
                         shutil.disk_usage(root).free)
    if result['status'] != 'BLOCKED_UNKNOWN_DURATION':
        credit=verified_credit(root)
        result['full_workflow_budget_bytes']=result['required_free_bytes']
        result['verified_existing_artifact_bytes']=credit
        result['required_free_bytes']=max(result['evidence_reserve_bytes'],result['required_free_bytes']-credit)
        result['shortfall_bytes']=max(0,result['required_free_bytes']-result['free_bytes'])
        result['status']='PASS' if result['shortfall_bytes']==0 else 'BLOCKED_DISK'
    result.update(source_preflight_sha256=sha(pointer['report']), checked_utc=utc())
    atomic(root/'preflight'/f'disk-{time.time_ns()}.json', result)
    atomic(root/'disk-preflight.json', result)
    if result['status'] != 'PASS':
        raise RuntimeError(f"Disk preflight blocked: {result}")
    return result


def checkpoint(root):
    """Derive progress without changing hash-bound production inputs."""
    if (root/'completion-status.json').exists():return load(root/'completion-status.json')
    manifest=load(root/'manifest.json')
    def document(name):return load(root/name) if (root/name).is_file() else {}
    action='preflight';state='INITIALIZED';detail='Resolve whole-series metadata before downloading'
    if document('source-preflight.json'):
        action='disk';state='SOURCE_PREFLIGHT_RECORDED';detail='Check metadata report and free space'
        if document('disk-preflight.json').get('status')=='PASS':action='download'
    if document('download/download_report.json').get('status')=='complete':
        action='prepare';state='SOURCE_DOWNLOADED';detail='Verify and create a fresh CapCut draft'
    if document('boundaries.json'):
        action='extract';state='AWAITING_SAVED_CHINESE_CAPTIONS';detail='Run Chinese Auto captions in CapCut, save the draft, then extract'
    if manifest.get('latest_export'):
        action='editorial-export';state='CAPTIONS_EXTRACTED';detail='Review raw extraction anomalies, then export editorial draft'
    if document('input.json'):
        action='editorial-import';state='AWAITING_EDITORIAL_REVIEW';detail='Fill contextual English and dispositions for every raw cue'
    if document('canonical-summary.json'):
        action='geometry-prepare';state='AWAITING_GEOMETRY';detail='Measure native caption geometry for every episode'
        if list((root/'geometry-evidence').glob('*/geometry-draft.json')):action='geometry-import'
    if document('geometry.json'):
        action='ocr';state='GEOMETRY_REVIEWED';detail='Extract selected proper-name midpoints and run GPU OCR'
        if document('active-name-ocr.json'):action='names-import';state='AWAITING_NAME_DECISIONS';detail='Review the OCR evidence and import explicit name decisions'
    if document('name-decisions.json').get('status') in ['APPLIED_BEFORE_TTS','NOT_REQUIRED']:
        action='speech';state='CANONICAL_READY';detail='Synthesize Ava and measure every cue fit'
        speech_status=document('speech-summary.json')
        if speech_status and (not speech_status.get('canonical_snapshot') or any(not Path(p).is_file() or sha(p)!=h for p,h in speech_status['canonical_snapshot'].items())):
            return {**manifest,'status':'CANONICAL_CHANGED','next_action':'speech','checkpoint_detail':'Previous speech is bound to different canonical files; revalidate names and synthesize current canonical'}
        if speech_status:
            if speech_status.get('status')!='PASS':
                return {**manifest,'status':'NEEDS_EDIT','next_action':'editorial-import','checkpoint_detail':'Speech fit requires a contextual editorial correction','needs_edit':speech_status.get('needs_edit',[])}
            action='background';state='SPEECH_READY';detail='Build hash-bound SFX-only backgrounds'
            if document('background-complete.json'):
                action='video';state='SFX_READY';detail='Render native episodes from the finalized canonical'
                active=document('active-video-generation.json')
                if active:
                    video_root=Path(active['run'])
                    snapshot=load(video_root/'dependency-snapshot.json')
                    if any(not Path(p).is_file() or sha(p)!=h for p,h in snapshot.items()):
                        return {**manifest,'status':'VIDEO_DEPENDENCIES_CHANGED','next_action':'revision','checkpoint_detail':'Record a revision ledger for the previous video generation before reusing encoded episodes'}
                    action='review-prepare';state='AWAITING_ENCODED_NAME_REVIEW';detail='View actual encoded name contacts and record explicit decisions'
                    review=load(video_root/'name-visual-review.json') if (video_root/'name-visual-review.json').exists() else {}
                    if review.get('status') in ['PASS','NOT_REQUIRED']:
                        action='pts';state='VIDEO_REVIEWED';detail='Verify source native PTS before final assembly'
                        if document('native-pts-qa-v002/report.json').get('status')=='PASS':
                            action='finish';state='NATIVE_PTS_READY';detail='Build the final native voice and SFX episode masters'
                            production=document('active-generation.json')
                            if production and (Path(production['run'])/'build-complete.json').is_file():
                                current=load(video_root/'dependency-snapshot.json');finished=load(Path(production['run'])/'dependency-snapshot.json')
                                if all(finished.get(k)==v for k,v in current.items()):
                                    action='package';state='ASSEMBLY_READY';detail='Package and run final combined media QA'
    if document('delivery.json'):action='seal';state='DELIVERY_READY';detail='Seal and verify the complete delivered tree'
    result={**manifest,'status':state,'next_action':action,'checkpoint_detail':detail}
    records=list((root/'executions').glob('*/result.json'))
    if records:
        last=load(max(records,key=lambda p:p.stat().st_mtime_ns))
        result['last_execution']={k:last[k] for k in ['action','status','exit_code','error_type','error'] if k in last}
    return result


def main():
    p=argparse.ArgumentParser(__doc__)
    actions=['init','preflight','disk','download','prepare','extract','status','resume','doctor','smoke',
             'editorial-export','editorial-import','geometry-prepare','geometry-import','ocr','names-import',
             'speech','background','video','revision','review-prepare','review-record','finish','pts','package','seal']
    p.add_argument('action',choices=actions)
    p.add_argument('--run',type=Path)
    p.add_argument('--config',type=Path,default=Path(os.environ.get('WORKFLOW_CONFIG',PROJECT/'config/local.json')))
    p.add_argument('--series-id');p.add_argument('--episodes')
    p.add_argument('--input',type=Path);p.add_argument('--reviewer');p.add_argument('--batch',type=Path)
    p.add_argument('--contacts',type=int,nargs='+');p.add_argument('--note');p.add_argument('--output',type=Path)
    p.add_argument('--online',action='store_true')
    args=p.parse_args()
    if args.action in ['doctor','smoke']:
        cfg=config(args.config)
        argv=[cfg['env']['controller_python'],'-X','utf8',str(PROJECT/'tools'/f'{args.action}.py'),'--config',str(args.config.resolve())]
        if args.output:argv+=['--output',str(args.output.resolve())]
        if args.online:argv+=['--online']
        raise SystemExit(subprocess.run(argv,cwd=PROJECT).returncode)
    if args.run is None:p.error('--run is required for this action')
    root=args.run.resolve()
    if args.action in ['status','resume']:
        if not root.is_dir():raise FileNotFoundError(root)
        doc=checkpoint(root)
        if args.action=='resume':
            doc={**doc,'resume_policy':'Checkpoint report only. Run the displayed next action explicitly; editorial and visual decisions require actual review.'}
        print(json.dumps(doc,ensure_ascii=False,indent=2));return
    if (root/'completion-status.json').exists():raise ValueError('Completed run is immutable. Create a new versioned run.')
    if args.action not in ['seal','package'] and (root/'delivery.json').exists():raise ValueError('Delivery already created; seal it or create a new run version')
    if args.action=='init':
        if not args.series_id or not args.series_id.isdigit() or not args.episodes:p.error('init needs numeric --series-id and --episodes 1-N')
        from season_capcut import episodes
        episodes(args.episodes)
        if root.exists():raise FileExistsError('Run already exists; use status/resume')
        cfg=config(args.config);env=cfg['env']
    else:
        if not root.is_dir():raise FileNotFoundError(root)
        manifest=load(root/'manifest.json');env=manifest['environment']
        args.config=Path(manifest.get('configuration',args.config))
    script='season_capcut.py';extra=[args.action];python=env['controller_python']
    if args.action=='init':extra+=['--series-id',args.series_id,'--episodes',args.episodes,'--config',str(args.config.resolve())]
    elif args.action=='preflight':script='season_source_preflight.cjs';extra=[]
    elif args.action=='ocr':script='season_name_ocr.py';extra=[];python=env['paddle_python']
    elif args.action=='speech':script='season_speech.py';extra=[];python=env['edge_python']
    elif args.action=='prepare':python=env['capcut_python']
    elif args.action in ['background','video']:script='season_media.py'
    elif args.action in ['editorial-export','editorial-import','geometry-prepare','geometry-import','names-import']:
        script='season_editorial.py'
        if args.input:extra+=['--input',str(args.input.resolve())]
        if args.reviewer:extra+=['--reviewer',args.reviewer]
    elif args.action in ['review-prepare','review-record']:
        script='season_visual_review.py';extra=[args.action.split('-')[1]]
        for key in ['batch','note','reviewer']:
            if getattr(args,key) is not None:extra+=['--'+key,str(getattr(args,key))]
        if args.contacts:extra+=['--contacts',*map(str,args.contacts)]
    elif args.action in ['revision','finish','pts','package']:script=f'season_{args.action}.py';extra=[]
    elif args.action=='seal':script='season_delivery_audit.py';extra=[]
    attempt=(PROJECT/'executions' if args.action=='init' else root/'executions')/f'{args.action}-{time.time_ns()}'
    attempt.mkdir(parents=True)
    started=time.monotonic();record=dict(action=args.action,begin_utc=utc(),status='RUNNING',run=str(root))
    atomic(attempt/'begin.json',record)
    code=1
    try:
        initial_runtime=runtime_lock(env) if args.action=='init' else None
        if args.action!='init':
            for document in ['workflow-lock.json','runtime-lock.json']:
                locked=load(root/document)
                selected=runtime_files_for_stage(args.action,locked['files'],locked.get('file_roles')) if document=='runtime-lock.json' else locked['files']
                for path,expected in selected.items():
                    if sha(path)!=expected:raise ValueError(f'Locked dependency changed: {path}; preserve this run and create a versioned run')
        if args.action in ['disk','download','background','video','finish','package']:check_disk(root)
        if args.action=='disk':code=0
        else:
            argv=([env['node'],str(PROJECT/'tools'/script),str(root)] if args.action=='preflight' else [python,'-X','utf8',str(PROJECT/'tools'/script),*extra,'--run',str(root)])
            record.update(argv=argv,worker_sha256=sha(PROJECT/'tools'/script),entrypoint_sha256=sha(__file__))
            atomic(attempt/'begin.json',record)
            with (attempt/'stdout.log').open('w',encoding='utf-8') as out,(attempt/'stderr.log').open('w',encoding='utf-8') as err:
                code=subprocess.run(argv,cwd=PROJECT,stdout=out,stderr=err,env={**os.environ,'WORKFLOW_CONFIG':str(args.config),'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}).returncode
            print((attempt/'stdout.log').read_text(encoding='utf-8')[-12000:])
            if code:print((attempt/'stderr.log').read_text(encoding='utf-8')[-6000:],file=sys.stderr)
            if code==0 and args.action=='init':
                atomic(root/'runtime-lock.json',initial_runtime)
                atomic(root/'workflow-lock.json',dict(files={**workflow_files(),str(args.config.resolve()):sha(args.config)},created_utc=utc()))
        record.update(status='COMPLETE' if code==0 else 'FAILED',exit_code=code)
    except BaseException as exc:
        record.update(status='FAILED',error_type=type(exc).__name__,error=str(exc));raise
    finally:
        record.update(end_utc=utc(),seconds=time.monotonic()-started)
        atomic(attempt/'result.json',record)
    if code:raise SystemExit(code)


if __name__=='__main__':main()
