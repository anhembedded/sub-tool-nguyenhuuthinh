"""Hash-bound visual review for every cue on an actual encoded video.

Layout assertions are automated; Chinese coverage and actual visible English
remain explicit visual review items. Never infer visual PASS from metadata.
"""
import math
import time
from fractions import Fraction
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from .common import atomic, sha
from . import media

CHECKS=('chinese_covered','english_correct','no_clipping','max_two_lines','position_correct')

def extract(video,source,cues,layout_path,out,env,selection_policy=None):
    import json
    tick=time.perf_counter();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    layout=json.loads(Path(layout_path).read_text(encoding='utf-8'))
    if layout['source_sha256']!=source['sha256']:raise ValueError('Layout source mismatch')
    rows={r['cue_id']:r for r in layout['cues']}
    if selection_policy not in (None,'proper-name-cues-only'):raise ValueError('Unknown QA selection policy')
    if selection_policy is None and len(rows)!=len(cues):raise ValueError('Layout cue count mismatch')
    if not cues:raise ValueError('No selected cues; record NONE_SELECTED without image extraction')
    if len({c['cue_id'] for c in cues})!=len(cues) or any(c['cue_id'] not in rows for c in cues):raise ValueError('Invalid selected cue identities')
    points=[];items=[]
    fps=Fraction(source['fps'])
    for c in cues:
        # Exact requested midpoint; floor to a real frame within [start,end).
        midpoint=Fraction(c['start_frame']+c['end_frame_exclusive'],2)/fps
        frame=min(c['end_frame_exclusive']-1,math.floor(midpoint*fps))
        if not c['start_frame']<=frame<c['end_frame_exclusive']:raise ValueError('Midpoint frame outside cue')
        r=rows[c['cue_id']]
        if r['canonical_text']!=c['canonical_text']:raise ValueError('Layout text differs from canonical text')
        errors=[]
        box=layout['blur_box'];ink=r['actual_ink_bbox']
        if len(r['lines'])>2:errors.append('LAYOUT_MORE_THAN_TWO_LINES')
        if not (0<=ink[0]<ink[2]<=box['width'] and 0<=ink[1]<ink[3]<=box['height']):errors.append('LAYOUT_CLIPPING')
        if max(map(abs,r['center_error_pixels']))>.5:errors.append('LAYOUT_POSITION')
        points.append(frame)
        items.append(dict(episode=source.get('episode'),cue_id=c['cue_id'],timestamp_seconds=float(midpoint),frame=frame,canonical_text=c['canonical_text'],lines=r['lines'],layout_errors=errors,checks={k:'PENDING' for k in CHECKS},status='AWAITING_VISUAL_REVIEW'))
    unique=sorted(set(points));frames=out/'frames';frames.mkdir()
    expr='+'.join(f'eq(n\\,{n})' for n in unique)
    media._run([env['ffmpeg'],'-v','error','-nostdin','-n','-threads','2','-i',video,'-vf',f'select={expr}','-fps_mode','vfr',frames/'frame-%05d.png'],out/'logs')
    images=sorted(frames.glob('frame-*.png'))
    if len(images)!=len(unique):raise ValueError('Rendered frame extraction incomplete')
    mapping=dict(zip(unique,images));preview_font=env.get('preview_font') or env.get('font')
    font=ImageFont.truetype(str(preview_font),19) if preview_font else ImageFont.load_default()
    contacts=[]
    for offset in range(0,len(items),6):
        chunk=items[offset:offset+6];canvas=Image.new('RGB',(1600,6*220),'#202020');draw=ImageDraw.Draw(canvas)
        for j,item in enumerate(chunk):
            path=mapping[item['frame']];item.update(image=str(path),image_sha256=sha(path))
            with Image.open(path) as im:
                if im.size!=(source['width'],source['height']):raise ValueError('Native dimensions changed')
                im=im.convert('RGB');thumb=im.copy();thumb.thumbnail((300,169))
                y=j*220;canvas.paste(thumb,(0,y+40))
                box=layout['blur_box'];top=max(0,box['y']-60);bottom=min(im.height,box['y']+box['height']+54)
                band=im.crop((0,top,im.width,bottom));band.thumbnail((1280,160))
                canvas.paste(band,(310,y+45))
            draw.text((8,y+5),f"EP{item['episode']} {item['cue_id']} {item['timestamp_seconds']:.3f}s",font=font,fill='white')
            text=item['canonical_text']
            draw.text((310,y+10),text[:115],font=font,fill='#ffdd00')
            if len(text)>115:draw.text((310,y+185),text[115:],font=font,fill='#ffdd00')
        contact=out/f'contact-{offset//6+1:03d}.jpg';canvas.save(contact,quality=95);contacts.append(str(contact))
    report=dict(status='AWAITING_VISUAL_REVIEW',video=str(Path(video).resolve()),video_sha256=sha(video),source_sha256=source['sha256'],layout=str(Path(layout_path).resolve()),layout_sha256=sha(layout_path),midpoint_formula='(start + end) / 2',cue_count=len(cues),unique_frames=len(unique),contacts=contacts,items=items,seconds=time.perf_counter()-tick,files=[])
    report['selection_policy']=selection_policy or 'all-cues'
    report['layout_cue_count']=len(rows)
    report['files']=[str(p) for p in out.rglob('*') if p.is_file()]+[str(out/'review.json')]
    atomic(out/'review.json',report)
    return report

def record_review(path,decisions,reviewer,output_path=None):
    import json
    if not isinstance(reviewer,str) or not reviewer.strip():raise ValueError('Actual reviewer identity required')
    path=Path(path);r=json.loads(path.read_text(encoding='utf-8'))
    if sha(r['video'])!=r['video_sha256'] or sha(r['layout'])!=r['layout_sha256']:raise ValueError('Video/layout changed; regenerate QA')
    if set(decisions)!={i['cue_id'] for i in r['items']}:raise ValueError('Each cue needs explicit decisions')
    errors=[]
    for item in r['items']:
        if sha(item['image'])!=item['image_sha256']:raise ValueError('Review image changed')
        d=decisions[item['cue_id']]
        if set(d.get('checks',{}))!=set(CHECKS) or any(type(v)is not bool for v in d['checks'].values()):raise ValueError('Explicit boolean visual checks required')
        item['checks']=d['checks'];item['note']=d.get('note','')
        item['status']='FAIL' if item['layout_errors'] or not all(d['checks'].values()) else 'PASS'
        if item['status']=='FAIL':
            if not item['note'].strip():raise ValueError('Failed visual cue needs an actionable correction note')
            errors.append(dict(episode=item['episode'],cue_id=item['cue_id'],timestamp_seconds=item['timestamp_seconds'],frame=item['frame'],checks=item['checks'],note=item['note'],rerender_scope=f"episode {item['episode']}"))
    r.update(status='FAIL' if errors else 'PASS',reviewer=reviewer,errors=errors)
    destination=Path(output_path) if output_path else path
    atomic(destination,r);atomic(destination.parent/(destination.stem+'-errors.json'),dict(video_sha256=r['video_sha256'],errors=errors))
    return r
