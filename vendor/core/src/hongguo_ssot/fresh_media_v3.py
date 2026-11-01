"""Silence-aware parallel video encoding and bounded-memory whole-file QA.

Video-only chunks are concatenated before the one continuous 48kHz audio mux.
The v2 renderer and its acceptance gates are left unchanged.
"""
from __future__ import annotations
from array import array
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import struct
import threading
import wave

from .fresh_media import (execute, samples_at_frame, pcm_info, validate_layout,
                          validate_cues, raster_subtitles, subtitle_track)
from .output_metadata import FINAL_METADATA_OPTIONS, audit_output_metadata
from .storage import atomic_json, file_hash, guard_output


def pcm_header(path, channels=None, samples=None, *, subtype="PCM_16"):
    """Validate bounded-memory 48kHz PCM16 or finite IEEE float32 RIFF/RF64.

    PCM16 remains the default final-master contract. Float intermediates must
    request ``FLOAT`` explicitly; concat uses ``None`` to discover the subtype.
    Floating-point headroom is retained without a [-1, 1] range restriction.
    """
    if subtype not in (None,"PCM_16","FLOAT"): raise ValueError("Unsupported PCM subtype")
    with Path(path).open("rb") as f:
        header=f.read(12)
        if header[:4] not in (b"RIFF",b"RF64") or header[8:12]!=b"WAVE":
            raise ValueError("Expected RIFF/RF64 PCM")
        data64=None;fmt=None
        while True:
            chunk=f.read(8)
            if len(chunk)!=8: raise ValueError("Incomplete WAV header")
            tag,size=struct.unpack("<4sI",chunk)
            if tag==b"ds64":
                values=f.read(size)
                if size<28: raise ValueError("Invalid RF64 ds64")
                data64=struct.unpack_from("<Q",values,8)[0]
            elif tag==b"fmt ":
                raw=f.read(size)
                if size<16 or len(raw)!=size: raise ValueError("Invalid PCM format")
                codec,chs,rate,byte_rate,align,bits=struct.unpack_from("<HHIIHH",raw)
                channel_mask=None
                if codec==65534:
                    if len(raw)<40 or struct.unpack_from("<H",raw,16)[0]<22:
                        raise ValueError("Invalid extensible PCM format")
                    valid_bits,channel_mask=struct.unpack_from("<HI",raw,18)
                    guid=raw[24:40]
                    codec=struct.unpack_from("<I",guid)[0]
                    if guid[4:]!=bytes.fromhex("00001000800000aa00389b71") or valid_bits!=bits:
                        raise ValueError("Unsupported extensible PCM subtype")
                actual={(1,16):"PCM_16",(3,32):"FLOAT"}.get((codec,bits))
                if (not actual or chs<=0 or rate!=48000 or align!=chs*(bits//8)
                        or byte_rate!=rate*align):
                    raise ValueError("Expected PCM16 or IEEE float32 48kHz")
                if subtype is not None and actual!=subtype: raise ValueError("PCM subtype differs")
                fmt={"channels":chs,"sample_rate":rate,"block_align":align,
                     "bits_per_sample":bits,"format_tag":codec,"subtype":actual,
                     "channel_mask":channel_mask}
            elif tag==b"data":
                if fmt is None: raise ValueError("PCM data precedes format")
                length=data64 if size==0xffffffff else size
                if length is None or length%fmt["block_align"]: raise ValueError("Invalid PCM length")
                count=length//fmt["block_align"]
                if f.tell()+length>Path(path).stat().st_size: raise ValueError("Truncated PCM")
                if channels is not None and channels!=fmt["channels"]: raise ValueError("PCM channels differ")
                if samples is not None and samples!=count: raise ValueError("PCM samples differ")
                result={**fmt,"samples":count,"data_offset":f.tell(),"data_bytes":length}
                if actual=="FLOAT":
                    remaining=length;peak=0.0;outside=0
                    while remaining:
                        block=f.read(min(384000,remaining))
                        if not block or len(block)%4: raise ValueError("Truncated float PCM")
                        values=array("f");values.frombytes(block)
                        import sys
                        if sys.byteorder!="little": values.byteswap()
                        for value in values:
                            if not math.isfinite(value): raise ValueError("Non-finite float PCM")
                            peak=max(peak,abs(value))
                            outside+=int(value < -1.0 or value > 32767/32768)
                        remaining-=len(block)
                    result.update(peak_absolute=peak,out_of_pcm16_range_samples=outside)
                return result
            else: f.seek(size,1)
            if size%2: f.seek(1,1)


def silence_chunks(total_frames, fps, occupied, *, min_seconds=120, max_seconds=300,
                   guard_ms=500, episode_boundaries=()):
    """Cut on frame boundaries only outside the union of OCR/ASR/actual TTS.

    If no safe gap exists in the target window, extend to the first safe gap;
    never cut ongoing speech just to satisfy the preferred chunk length.
    """
    rate=Fraction(fps)
    if total_frames<=0 or rate<=0 or not 0<min_seconds<=max_seconds or guard_ms<0:
        raise ValueError("Invalid chunk timeline")
    guard=math.ceil(guard_ms*rate/1000)
    intervals=[]
    for start,end in occupied:
        if not 0<=start<end<=total_frames: raise ValueError("Occupied interval outside timeline")
        intervals.append((max(0,start-guard),min(total_frames,end+guard)))
    merged=[]
    for start,end in sorted(intervals):
        if merged and start<=merged[-1][1]: merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else: merged.append((start,end))
    def safe(candidate):
        for start,end in merged:
            if start<=candidate<end: candidate=end
        return candidate
    bounds=[0,*sorted(set(episode_boundaries)),total_frames]
    if bounds!=sorted(set(bounds)) or bounds[0]!=0 or bounds[-1]!=total_frames:
        raise ValueError("Invalid episode boundaries")
    chunks=[]
    for begin,end in zip(bounds,bounds[1:]):
        cursor=begin
        while end-cursor>max_seconds*rate:
            low=cursor+math.ceil(min_seconds*rate)
            high=cursor+math.floor(max_seconds*rate)
            # Prefer the first safe frame near the upper target, then an earlier gap.
            candidates=[safe(high),safe(low)]
            candidates.extend(safe(b) for a,b in merged if low<=b<=high)
            within=[c for c in candidates if low<=c<=high]
            cut=max(within) if within else safe(high)
            if cut>=end: break
            chunks.append({"start_frame":cursor,"end_frame_exclusive":cut})
            cursor=cut
        chunks.append({"start_frame":cursor,"end_frame_exclusive":end})
    return chunks


def probe(path, ffprobe):
    p=subprocess.run([str(ffprobe),"-v","error","-show_streams","-show_format","-of","json",str(path)],
                     capture_output=True,text=True,timeout=120,check=True)
    if p.stderr.strip(): raise ValueError("Probe reports damaged media: "+p.stderr[:500])
    return json.loads(p.stdout)


def streaming_video_qa(path, total_frames, fps, output_dir, ffprobe):
    output=Path(output_dir)
    output.mkdir(parents=True,exist_ok=True)
    rate=Fraction(fps)
    count=0
    max_error=0.0
    previous=None
    timeout=max(120,float(total_frames/rate)*4)
    with (output/"frame-probe.stderr.log").open("w+",encoding="utf8") as errors:
        command=[str(ffprobe),"-v","error","-select_streams","v:0","-show_frames",
                 "-show_entries","frame=best_effort_timestamp_time","-of","csv=p=0",str(path)]
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,text=True)
        timer=threading.Timer(timeout,process.kill)
        timer.start()
        try:
            for line in process.stdout:
                head=line.strip().split(",")[0]
                if not head: continue
                try: pts=float(head)
                except ValueError: raise ValueError("Missing/invalid video PTS")
                expected=float(Fraction(count,1)/rate)
                error=abs(pts-expected)
                max_error=max(max_error,error)
                if not math.isfinite(pts) or (previous is not None and pts<=previous) or error>max(.0001,float(1/rate)/20):
                    raise ValueError(f"Discontinuous video PTS at frame {count}")
                count+=1
                previous=pts
            process.wait()
            errors.flush(); errors.seek(0)
            error_text=errors.read(2000)
            if process.returncode or error_text.strip():
                raise ValueError("Streaming frame decode failed: "+error_text)
        finally:
            timer.cancel()
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close()
    if count!=total_frames: raise ValueError(f"Frame count differs: {count} != {total_frames}")
    return {"status":"PASS","frames":count,"max_pts_error_seconds":max_error,
            "memory_policy":"one ffprobe row at a time"}


def streaming_audio_qa(path, expected_samples, output_dir, ffmpeg, ffprobe):
    output=Path(output_dir)
    output.mkdir(parents=True,exist_ok=True)
    info=probe(path,ffprobe)
    audio=[s for s in info["streams"] if s["codec_type"]=="audio"]
    if len(audio)!=1 or int(audio[0]["sample_rate"])!=48000 or audio[0]["channels"]!=2:
        raise ValueError("Expected one continuous 48kHz stereo audio stream")
    stream=audio[0]
    container_samples=round(Fraction(int(stream["duration_ts"]))*Fraction(stream["time_base"])*48000)
    if container_samples!=expected_samples:
        raise ValueError(f"Audio container timeline mismatch: {container_samples} != {expected_samples}")
    peak=energy=0.0
    samples=0
    with (output/"audio-decode.stderr.log").open("w+",encoding="utf8") as errors:
        p=subprocess.Popen([str(ffmpeg),"-v","error","-xerror","-nostdin","-i",str(path),"-map","0:a:0",
                            "-f","f32le","-acodec","pcm_f32le","-ac","2","-ar","48000","pipe:1"],
                           stdout=subprocess.PIPE,stderr=errors)
        timer=threading.Timer(max(120,expected_samples/48000*4),p.kill);timer.start()
        remainder=b""
        try:
            while block:=p.stdout.read(48000*2*4):
                block=remainder+block
                usable=len(block)//8*8
                remainder=block[usable:]
                values=array("f");values.frombytes(block[:usable])
                import sys
                if sys.byteorder!="little": values.byteswap()
                for value in values:
                    if not math.isfinite(value): raise ValueError("Non-finite decoded audio")
                    peak=max(peak,abs(value));energy+=value*value
                samples+=len(values)//2
            p.wait();errors.flush();errors.seek(0)
            if remainder or p.returncode or errors.read(2000).strip(): raise ValueError("Streaming PCM decode failed")
        finally:
            timer.cancel()
            if p.poll() is None: p.kill();p.wait()
            p.stdout.close()
    padding=samples-expected_samples
    if not 0<=padding<=1024: raise ValueError("Decoded audio duration differs beyond AAC padding")
    return {"status":"PASS","container_samples":container_samples,"decoded_samples":samples,
            "aac_padding_samples":padding,"peak":peak,"rms":math.sqrt(energy/max(1,samples*2)),
            "listening_review":"NEEDS_REVIEW","memory_policy":"<= 384kB PCM blocks"}


def concat_pcm(inputs, output, channels):
    """Concatenate matching 48kHz PCM formats byte-for-byte, retaining headroom."""
    output=Path(output)
    if output.exists(): raise FileExistsError(output)
    inputs=list(inputs)
    if not inputs: raise ValueError("PCM concat requires inputs")
    rows=[pcm_header(p,channels=channels,subtype=None) for p in inputs]
    fields=("channels","sample_rate","block_align","bits_per_sample","format_tag","subtype","channel_mask")
    if any(any(r[key]!=rows[0][key] for key in fields) for r in rows[1:]):
        raise ValueError("PCM concat formats differ")
    fmt=rows[0]
    total=sum(r["samples"] for r in rows)
    size=total*fmt["block_align"]
    if fmt["channel_mask"] is not None:
        guid=struct.pack("<I",fmt["format_tag"])+bytes.fromhex("00001000800000aa00389b71")
        extra=struct.pack("<HHI",22,fmt["bits_per_sample"],fmt["channel_mask"])+guid
        codec=65534
    else:
        extra=b"\0\0" if fmt["subtype"]=="FLOAT" else b""
        codec=fmt["format_tag"]
    format_bytes=struct.pack("<HHIIHH",codec,channels,48000,48000*fmt["block_align"],
                             fmt["block_align"],fmt["bits_per_sample"])+extra
    fact=b"fact"+struct.pack("<II",4,min(total,0xffffffff)) if fmt["subtype"]=="FLOAT" else b""
    header_size=12+36+8+len(format_bytes)+len(fact)+8
    with output.open("xb") as writer:
        # Always RF64, including small fixtures, to exercise the long-file path.
        writer.write(b"RF64"+struct.pack("<I",0xffffffff)+b"WAVEds64"+struct.pack("<IQQQI",28,size+header_size-8,size,total,0))
        writer.write(b"fmt "+struct.pack("<I",len(format_bytes))+format_bytes+fact)
        writer.write(b"data"+struct.pack("<I",0xffffffff))
        for path,row in zip(inputs,rows):
            with Path(path).open("rb") as reader:
                reader.seek(row["data_offset"])
                remaining=row["data_bytes"]
                while remaining:
                    block=reader.read(min(384000,remaining))
                    if not block: raise ValueError("PCM disappeared during concat")
                    writer.write(block);remaining-=len(block)
    return total


def render_video_chunk(source, source_offset, chunk, cues, profile, root, ffmpeg, ffprobe, *, threads=8, preset="medium"):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    start,end=chunk["start_frame"],chunk["end_frame_exclusive"]
    count=end-start
    local=[]
    for cue in cues:
        if cue["start_frame"]<end and cue["end_frame_exclusive"]>start:
            if cue["start_frame"]<start or cue["end_frame_exclusive"]>end:
                raise ValueError("Chunk would cut a subtitle")
            local.append({**cue,"start_frame":cue["start_frame"]-start,"end_frame_exclusive":cue["end_frame_exclusive"]-start})
    if local: validate_cues(local,count)
    raster=raster_subtitles(local,profile,root/"raster")
    subtrack=subtitle_track(local,raster,profile,count,root/"track")
    assets=[profile["branding"][k] for k in ("logo","wordmark")]
    rate=Fraction(profile["fps"])
    source_video=next(s for s in probe(source,ffprobe)["streams"] if s["codec_type"]=="video")
    # Accurate input seek during re-encoding; OCR refinement never uses this path.
    argv=[str(ffmpeg),"-hide_banner","-v","error","-xerror","-nostdin","-n","-threads","2",
          "-ss",f"{float(Fraction(source_offset+start,1)/rate):.12f}","-i",str(source)]
    for asset in assets: argv += ["-i",str(asset["path"])]
    argv += ["-f","concat","-safe","0","-i",str(subtrack)]
    blur,sub=profile["blur"],profile["subtitles"]
    radius=min(int(blur.get("radius",12)),max(1,(min(blur["width"],blur["height"])-1)//2))
    chroma=max(0,min(radius,min(blur["width"],blur["height"])//4-1))
    graph=[f"[0:v]trim=end_frame={count},setpts=N/({rate.numerator}/{rate.denominator}*TB),split=2[canvas][roi]",
           f"[roi]crop={blur['width']}:{blur['height']}:{blur['x']}:{blur['y']}:exact=1,boxblur=luma_radius={radius}:luma_power=2:chroma_radius={chroma}:chroma_power=2[blurred]",
           f"[canvas][blurred]overlay={blur['x']}:{blur['y']}:eof_action=pass[base]"]
    prior="base"
    for i,asset in enumerate(assets,1):
        graph.append(f"[{i}:v]format=rgba,scale={asset['width']}:{asset['height']}:flags=lanczos[b{i}]")
        graph.append(f"[{prior}][b{i}]overlay={asset['x']}:{asset['y']}:eof_action=repeat:repeatlast=1[o{i}]")
        prior=f"o{i}"
    graph.append(f"[{prior}][3:v]overlay={sub['x']}:{sub['y']}:eof_action=repeat:repeatlast=1,trim=end_frame={count},format=yuv420p[vout]")
    graph_path=root/"filtergraph.txt";graph_path.write_text(";\n".join(graph),encoding="utf8")
    path=root/"video.mp4"
    argv += ["-filter_complex_threads","2","-/filter_complex",str(graph_path),"-map","[vout]","-an",
             "-frames:v",str(count),"-fps_mode","passthrough","-c:v","libx264","-preset",preset,
             "-threads",str(threads),"-crf","21","-maxrate","6M","-bufsize","12M","-pix_fmt","yuv420p",
             "-force_key_frames","0","-video_track_timescale",str(rate.numerator)]
    for field,option in (("color_range","-color_range"),("color_space","-colorspace"),
                         ("color_transfer","-color_trc"),("color_primaries","-color_primaries")):
        if source_video.get(field) not in (None,"unknown","unspecified"):
            argv += [option,source_video[field]]
    if source_video.get("color_range") in ("tv","pc"):
        argv += ["-bsf:v","h264_metadata=video_full_range_flag="+str(int(source_video["color_range"]=="pc"))]
    argv += [*FINAL_METADATA_OPTIONS,str(path)]
    execute(argv,root/"logs",timeout=max(300,float(count/rate)*20))
    qa=streaming_video_qa(path,count,profile["fps"],root/"qa",ffprobe)
    actual=next(s for s in probe(path,ffprobe)["streams"] if s["codec_type"]=="video")
    for field in ("width","height","pix_fmt","sample_aspect_ratio","color_range","color_space","color_transfer","color_primaries"):
        expected=source_video.get(field)
        if expected not in (None,"unknown","unspecified") and actual.get(field)!=expected:
            raise ValueError("Native visual descriptor changed: "+field)
    receipt={"path":str(path),"sha256":file_hash(path),"chunk":chunk,"qa":qa}
    atomic_json(root/"receipt.json",receipt)
    return receipt


def concat_video(chunks, output, ffmpeg, root):
    listing=Path(root)/"video.ffconcat"
    lines=["ffconcat version 1.0"]
    for row in chunks:
        path=Path(row["path"])
        if file_hash(path)!=row["sha256"]: raise ValueError("Chunk changed before concat")
        escaped=str(path.resolve()).replace("\\","/").replace("'","'\\''")
        lines.append(f"file '{escaped}'")
    listing.write_text("\n".join(lines)+"\n",encoding="utf8")
    execute([ffmpeg,"-v","error","-xerror","-nostdin","-n","-f","concat","-safe","0","-i",listing,
             "-map","0:v:0","-c:v","copy","-an",*FINAL_METADATA_OPTIONS,output],Path(root)/"concat-logs",timeout=86400)


def render_episode(source, source_offset, total_frames, cues, occupied, voice_wav, background_wav,
                   profile, output_dir, *, ffmpeg, ffprobe, workers=3, threads=8,preset="medium",
                   min_seconds=120,max_seconds=300):
    root=Path(output_dir);root.mkdir(parents=True,exist_ok=False)
    if workers not in (1,2,3,4) or threads<1 or workers*threads>32 or preset not in ("fast","medium"):
        raise ValueError("Unsupported CPU render budget")
    validate_layout(profile)
    if cues: validate_cues(cues,total_frames)
    expected=samples_at_frame(total_frames,profile["fps"])
    pcm_header(Path(voice_wav),channels=1,samples=expected)
    pcm_header(Path(background_wav),channels=2,samples=expected,subtype=None)
    chunks=silence_chunks(total_frames,profile["fps"],occupied,min_seconds=min_seconds,max_seconds=max_seconds)
    atomic_json(root/"chunk_plan.json",chunks)
    def render(pair):
        i,c=pair
        return render_video_chunk(source,source_offset,c,cues,profile,root/f"chunk-{i:05d}",ffmpeg,ffprobe,threads=threads,preset=preset)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        receipts=list(pool.map(render,enumerate(chunks)))
    joined=root/"joined-video.mp4"
    concat_video(receipts,joined,ffmpeg,root)
    streaming_video_qa(joined,total_frames,profile["fps"],root/"concat-qa",ffprobe)
    output=root/"video_DRAFT.mp4"
    mixed=root/"mixed48.wav"
    mix=f"[1:a]volume=-15dB[bg];[2:a]pan=stereo|c0=c0|c1=c0[voice];[bg][voice]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,alimiter=limit=0.89:level=0:latency=1,apad=whole_len={expected},atrim=end_sample={expected},asetpts=N/SR/TB[a]"
    execute([ffmpeg,"-v","error","-xerror","-nostdin","-n","-i",joined,"-i",background_wav,"-i",voice_wav,
             "-filter_complex",mix,"-map","[a]","-vn","-c:a","pcm_s16le","-ar","48000","-ac","2","-rf64","auto",mixed],
             root/"mix-logs",timeout=max(300,expected/48000*5))
    pcm_header(mixed,channels=2,samples=expected)
    execute([ffmpeg,"-v","error","-xerror","-nostdin","-n","-i",joined,"-i",mixed,
             "-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-b:a","192k",
             "-ar","48000","-ac","2","-movie_timescale","48000","-movflags","+faststart",
             *FINAL_METADATA_OPTIONS,output],root/"mux-logs",timeout=max(300,expected/48000*5))
    video=streaming_video_qa(output,total_frames,profile["fps"],root/"final-video-qa",ffprobe)
    audio=streaming_audio_qa(output,expected,root/"final-audio-qa",ffmpeg,ffprobe)
    metadata=audit_output_metadata(output,root/"metadata.json",ffprobe=ffprobe)
    result={"output":str(output),"output_sha256":file_hash(output),"technical_PASS":True,
            "audio_master":str(mixed),"audio_master_sha256":file_hash(mixed),
            "status":"DRAFT","video":video,"audio":audio,"metadata":metadata,"chunks":receipts,
            "semantic_review":"NEEDS_REVIEW","listening_review":"NEEDS_REVIEW"}
    atomic_json(root/"qa_receipt.json",result)
    return result


def rolling_cleanup(root, files, final_output, expected_sha256, qa):
    """Delete only exact, caller-enumerated temporaries after final verified concat."""
    if qa.get("technical_PASS") is not True or file_hash(Path(final_output))!=expected_sha256:
        raise ValueError("Cleanup requires current final-output hash and passing QA")
    allowed={".wav",".mp4"}
    paths=[]
    for item in files:
        p=guard_output(Path(item),Path(root),[Path(final_output)])
        if p.suffix.lower() not in allowed: raise ValueError("Only enumerated temporary WAV/video files may be cleaned")
        paths.append(p)
    removed=[]
    for p in paths:
        if p.is_file():
            removed.append({"path":str(p),"sha256":file_hash(p),"bytes":p.stat().st_size})
            p.unlink()
    return {"final_output_sha256":expected_sha256,"removed":removed}
