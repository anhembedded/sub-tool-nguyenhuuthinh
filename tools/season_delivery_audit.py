"""Seal a verified delivery; preserve actual execution coverage and failures."""
import argparse,shutil,sys
from datetime import datetime,timedelta
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,utc
from hongguo_lite.season_contracts import unsealed,episode_map

def main(root):
    unsealed(root)
    report=load(root/'delivery.json');delivery=Path(report['video']).parent
    if not all(report[k] is True for k in ['artifact_ready','technical_pass','name_qa_pass']):raise ValueError('Delivery gates not passed')
    if sha(report['video'])!=report['video_sha256'] or sha(report['srt'])!=report['srt_sha256']:raise ValueError('Delivery media changed')
    if (delivery/'SHA256SUMS.json').exists():raise FileExistsError('Already sealed; preserve delivery')
    if load(delivery/'delivery-report.json')!=report:raise ValueError('Delivery report differs from run report')
    from tools.season_package import preflight
    checked=preflight(root)
    if checked[1]['identity']!=report['generation']:raise ValueError('Delivery generation changed')
    for name,path in checked[7].items():
        if sha(delivery/name)!=sha(path):raise ValueError('Delivery evidence differs: '+name)
    lineage=load(delivery/'cue-lineage.json')
    if lineage!=dict(episodes=checked[10],generation=report['generation']):raise ValueError('Delivery cue lineage differs')
    source=load(root/'download/download_report.json');video=load(root/'active-video-generation.json');production=load(root/'active-generation.json')
    locations={root/'media-store',Path(video['run']),Path(production['run'])}
    locations.update(Path(p) for p in episode_map(Path(video['run']),report['episodes']).values())
    revision=root/'canonical-revision.json'
    if revision.exists():locations.add(Path(load(revision)['base_video']['run']))
    receipts=[];intervals=[];failures=[];seen=set()
    for location in locations:
        for p in location.rglob('receipt.json'):
            if p.resolve() in seen:continue
            seen.add(p.resolve())
            d=load(p)
            if not all(k in d for k in ['stage','end_utc','seconds']):continue
            end=datetime.fromisoformat(d['end_utc']);begin=end-timedelta(seconds=d['seconds']);intervals.append((begin,end))
            receipts.append(dict(path=str(p),sha256=sha(p),stage=d['stage'],seconds=d['seconds'],begin_utc=begin.isoformat(),end_utc=end.isoformat()))
        for p in location.rglob('failure.json'):
            d=load(p);failures.append(dict(path=str(p),sha256=sha(p),stage=d.get('stage'),error_type=d.get('error_type'),error=d.get('error'),seconds=d.get('seconds')))
    merged=[]
    for begin,end in sorted(intervals):
        if merged and begin<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((begin,end))
    executions=[dict(path=str(p),sha256=sha(p),record=load(p)) for p in sorted((root/'executions').glob('*/result.json'))]
    atomic(delivery/'stage-timings.json',dict(store_receipts=receipts,complete_stage_sum_seconds=sum(r['seconds'] for r in receipts),complete_stage_union_seconds=sum((b-a).total_seconds() for a,b in merged),executions=executions,limitations=['Store timings and CLI executions overlap; do not sum them as elapsed.','Manual recognition, translation and visual review have no measured duration unless separately recorded.']))
    for name in ['canonical-revision.json','prepare-qa.json','runner-tests.json','qa-adapter-tests.json','disk-preflight.json']:
        if (root/name).exists():shutil.copy2(root/name,delivery/name)
    atomic(delivery/'attempts.json',dict(preserved_stage_failures=failures,execution_failures=[e for e in executions if e['record']['status']!='COMPLETE'],qa_core_sha256=sha(PROJECT/'hongguo_lite/media.py')))
    plan=load(root/'input.json')
    if sha(plan['source_srt'])!=plan['source_srt_sha256']:raise ValueError('Original captions changed')
    manifest=dict(schema=1,completed_utc=utc(),project=str(PROJECT),series_id=report['series_id'],series_title=report['series_title'],episodes=report['episodes'],source_ledger=[dict(episode=e['episode'],video_id=e.get('vid'),sha256=e['validation']['sha256'],frames=e['validation']['frameCount'],fps=e['validation']['frameRate'],full_decode_exit_code=e['validation']['fullDecodeExitCode']) for e in source['episodes']],source_download_report_sha256=sha(root/'download/download_report.json'),raw_srt_sha256=plan['source_srt_sha256'],canonical_files={p.name:sha(p) for p in sorted((root/'translation').glob('episode*.json'))},video_generation=video['identity'],production_generation=production['identity'],dependencies_snapshot_sha256=sha(Path(production['run'])/'dependency-snapshot.json'),acceptance={k:report[k] for k in ['artifact_ready','technical_pass','name_qa_pass','listening_status','semantic_status','external_published']},video_sha256=report['video_sha256'],srt_sha256=report['srt_sha256'],included_stems=report['included_stems'],name_review_status=report['name_review_status'])
    atomic(delivery/'manifest.json',manifest)
    atomic(delivery/'SHA256SUMS.json',dict(algorithm='sha256',sealed_utc=utc(),files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}))
    for name,expected in load(delivery/'SHA256SUMS.json')['files'].items():
        if sha(delivery/name)!=expected:raise ValueError('Sealed payload changed: '+name)
    status=dict(status='DRAFT_DELIVERED',delivery=str(delivery),video=report['video'],srt=report['srt'],**manifest['acceptance'],delivery_checksums_sha256=sha(delivery/'SHA256SUMS.json'))
    atomic(root/'completion-status.json',status)
    print(delivery,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args();main(a.run.resolve())
