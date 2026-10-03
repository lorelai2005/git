#!/usr/bin/env python3
"""Timestamped, bounded video evidence. No LLM and no invented visual observations."""
from __future__ import annotations
import argparse, contextlib, hashlib, json, math, os, re, subprocess, sys, time, uuid
from pathlib import Path
from urllib.request import Request, urlopen

VERSION = 'av-timeline-1.0'
ROOT = Path('/data/benchmarks/_work')
MAX_DURATION = 600
MAX_FRAMES = 360
PAGE_SIZE = 9

def fail(code):
    raise ValueError(code)

def authorized_run(value, root=ROOT):
    root = root.resolve(strict=True)
    run = Path(value).resolve(strict=True)
    if run == root or not run.is_relative_to(root) or not run.is_dir(): fail('evidence_dir_outside_allowed_root')
    return run

def contained(parent, child):
    resolved = child.resolve()
    if not resolved.is_relative_to(parent.resolve()): fail('artifact_path_escape')
    return resolved

def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()

def atomic_json(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)

def run_cmd(args, timeout=150, cwd=None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if r.returncode: fail('media_command_failed: ' + Path(args[0]).name)
    return r

def probe(video):
    d = json.loads(run_cmd(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(video)]).stdout)
    streams = d.get('streams', [])
    v = next((x for x in streams if x.get('codec_type') == 'video'), None)
    a = next((x for x in streams if x.get('codec_type') == 'audio'), None)
    if v is None: fail('no_video_stream')
    duration = float(v.get('duration') or d['format']['duration'])
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION: fail('unsupported_video_duration')
    video_start = float(v.get('start_time', 0))
    offset = float(a.get('start_time', video_start)) - video_start if a else 0
    if not math.isfinite(video_start) or not math.isfinite(offset) or abs(offset) > duration: fail('invalid_stream_offset')
    return {'duration_sec': duration, 'audio_present': bool(a), 'audio_to_video_offset_sec': offset,
            'video_start_time_sec': video_start, 'width': v['width'], 'height': v['height']}

def normalize_segments(payload, duration, offset=0):
    rows = payload.get('segments')
    if not isinstance(rows, list) or not rows: fail('asr_has_no_timestamped_segments')
    out = []
    last_start = -1.0
    for row in rows:
        if not isinstance(row, dict): fail('invalid_asr_segment')
        text = row.get('text', '')
        if not isinstance(text, str): fail('invalid_asr_text')
        if not text.strip(): continue
        try:
            start, end = float(row['start']), float(row['end'])
        except (KeyError, ValueError, TypeError): fail('invalid_asr_timestamps')
        if not all(math.isfinite(x) for x in [start, end]) or start < 0 or end <= start or start < last_start:
            fail('invalid_asr_timestamps')
        last_start = start
        start, end = start + offset, end + offset
        if start > duration + 0.5 or end > duration + 1: fail('asr_timestamps_outside_video')
        start, end = max(0, start), min(duration, end)
        if end <= start: continue
        out.append({'id': f'A{len(out)+1:03}', 'start_sec': round(start, 3), 'end_sec': round(end, 3),
                    'text': text.strip(), 'evidence': 'machine_asr_segment', 'human_verified': False})
    if not out: fail('asr_has_no_timestamped_segments')
    return out

def request_asr(wav):
    base = os.environ.get('ASR_API_BASE', '')
    key = os.environ.get('ASR_API_KEY') or os.environ.get('API_KEY', '')
    if not base or not key: fail('timestamped_asr_not_configured')
    if wav.stat().st_size > 24*1024*1024: fail('audio_exceeds_upload_limit')
    boundary = 'renwei' + uuid.uuid4().hex
    chunks = []
    fields = {'model': os.environ.get('ASR_MODEL', 'Systran/faster-whisper-medium'), 'language': 'zh',
              'response_format': 'verbose_json', 'timestamp_granularities[]': 'segment'}
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(), wav.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
    req = Request(base.rstrip('/')+'/audio/transcriptions', data=b''.join(chunks),
                  headers={'Authorization': 'Bearer '+key, 'Content-Type': 'multipart/form-data; boundary='+boundary})
    try:
        with urlopen(req, timeout=180) as res:
            body = res.read(4*1024*1024+1)
        if len(body) > 4*1024*1024: fail('asr_response_too_large')
        payload = json.loads(body)
        if not isinstance(payload, dict): fail('invalid_asr_response')
        return payload
    except ValueError: raise
    except Exception: fail('timestamped_asr_request_failed')  # Never include URLs/credentials from exceptions.

def get_transcript(run, out, video, info, asr_runner=request_asr):
    if not info['audio_present']: return [], 'no_audio', [], ''
    errors = []
    for candidate in [out/'asr.verbose.json', run/'asr-response.json', run/'audio.json']:
        candidate = contained(run, candidate)
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding='utf-8'))
                rows = normalize_segments(data, info['duration_sec'], info['audio_to_video_offset_sec'])
                return rows, 'timestamped_asr', errors, str(candidate)
            except (ValueError, TypeError, KeyError): pass
    wav = contained(out, out/'audio.aligned.wav')
    run_cmd(['ffmpeg', '-y', '-nostdin', '-v', 'error', '-i', str(video), '-map', '0:a:0', '-vn',
             '-af', 'asetpts=PTS-STARTPTS', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav)])
    try:
        data = asr_runner(wav)
        atomic_json(out/'asr.verbose.json', data)
        rows = normalize_segments(data, info['duration_sec'], info['audio_to_video_offset_sec'])
        return rows, 'timestamped_asr', errors, str(out/'asr.verbose.json')
    except ValueError as exc:
        errors.append(str(exc) if re.fullmatch(r'[a-z_]+', str(exc)) else 'timestamped_asr_failed')
        return [], 'unavailable', errors, ''

def parse_frame_metadata(text):
    out = []
    current = None
    for line in text.splitlines():
        m = re.search(r'frame:\s*(\d+)\s+pts:\s*\S+\s+pts_time:\s*([\d.eE+\-]+)', line)
        if m:
            current = {'index': int(m[1]), 'time_sec': float(m[2]), 'scene_score': None}
            if not math.isfinite(current['time_sec']) or current['time_sec'] < 0: fail('invalid_frame_timestamp')
            out.append(current)
        elif current is not None and line.startswith('lavfi.scene_score='):
            score = float(line.partition('=')[2])
            if not math.isfinite(score): fail('invalid_scene_score')
            current['scene_score'] = score
    if not out: fail('no_timestamped_frames')
    if any(b['time_sec'] <= a['time_sec'] for a,b in zip(out, out[1:])): fail('unordered_frame_timestamps')
    return out

def extract_frames(video, out, duration):
    frames_dir = contained(out, out/'frames'); frames_dir.mkdir(exist_ok=True)
    metadata = out/'frame-metadata.txt'
    # Actual decoder PTS are retained; filenames are NOT treated as clocks.
    tail = max(0, duration-0.12)
    select = f"isnan(prev_selected_t)+gt(scene,0.30)+if(lt(t,3),gte(t-prev_selected_t,0.5),gte(t-prev_selected_t,2))+gte(t,{tail:.6f})*lt(prev_selected_t,{tail:.6f})"
    vf = f"setpts=PTS-STARTPTS,select='{select}',metadata=mode=print:file=frame-metadata.txt,scale=480:-2"
    run_cmd(['ffmpeg','-y','-nostdin','-v','error','-threads','1','-i',str(video),'-an','-vf',vf,
             '-fps_mode','vfr','-frames:v',str(MAX_FRAMES),'-q:v','3','-threads','1',str(frames_dir/'frame_%04d.jpg')],timeout=180,cwd=out)
    items = parse_frame_metadata(metadata.read_text())
    frames = []
    for row in items:
        file = frames_dir/f"frame_{row['index']+1:04}.jpg"
        if not file.is_file(): continue
        pts = row['time_sec']
        if pts > duration+0.1: fail('frame_after_video_end')
        frames.append({'id': f'F{len(frames)+1:03}', 'time_sec': round(pts, 4),
                       'scene_score': row['scene_score'], 'file': str(file),
                       'candidate_cut': bool(row['scene_score'] is not None and row['scene_score']>0.30)})
    if not frames: fail('no_decoded_frames')
    return frames

def create_sheets(frames, out):
    from PIL import Image, ImageOps, ImageDraw, ImageFont
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 20)
    pages = []
    for first in range(0, len(frames), PAGE_SIZE):
        batch = frames[first:first+PAGE_SIZE]
        # Three portrait columns; text label is outside the image, never occluding subtitles.
        cell_w, image_h, label_h = 320, 480, 30
        sheet = Image.new('RGB',(cell_w*3, (image_h+label_h)*math.ceil(len(batch)/3)), 'white')
        draw = ImageDraw.Draw(sheet)
        for i, frame in enumerate(batch):
            with Image.open(contained(out, Path(frame['file']))) as im:
                tile = ImageOps.contain(im.convert('RGB'), (cell_w,image_h))
            x,y = (i%3)*cell_w, (i//3)*(image_h+label_h)
            sheet.paste(tile,(x+(cell_w-tile.width)//2,y+label_h+(image_h-tile.height)//2))
            draw.text((x+5,y+3),f"{frame['id']}  {frame['time_sec']:.3f}s",font=font,fill='black')
        number = len(pages)+1
        file = out/f'sheet_{number:02}.jpg'
        sheet.save(file, quality=88)
        pages.append({'page': number, 'file': str(file), 'frame_ids':[f['id'] for f in batch],
                      'start_sec':batch[0]['time_sec'], 'end_sec':batch[-1]['time_sec']})
    return pages

def align_segments(segments, frames):
    rows = []
    for s in segments:
        inside = [f['id'] for f in frames if s['start_sec'] <= f['time_sec'] < s['end_sec']]
        before = [f for f in frames if f['time_sec'] < s['start_sec']]
        after = [f for f in frames if f['time_sec'] >= s['end_sec']]
        rows.append({**s, 'frame_ids':inside,
                     'context_before': before[-1]['id'] if before else None,
                     'context_after': after[0]['id'] if after else None})
    return rows

def srt_time(seconds):
    ms = round(seconds*1000)
    return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'

def write_transcript(rows,out):
    (out/'transcript.srt').write_text('\n\n'.join(f"{i}\n{srt_time(s['start_sec'])} --> {srt_time(s['end_sec'])}\n{s['text']}" for i,s in enumerate(rows,1))+'\n',encoding='utf-8')
    (out/'transcript.txt').write_text('\n'.join(s['text'] for s in rows)+'\n',encoding='utf-8')

def build_timeline(value, root=ROOT, asr_runner=request_asr):
    import fcntl
    run = authorized_run(value,root)
    video = contained(run,run/'video.mp4')
    if not video.is_file() or not 0 < video.stat().st_size <= 300*1024*1024: fail('missing_or_oversize_video')
    info = probe(video); digest = sha256(video)
    out = contained(run,run/'av-timeline-v1'); out.mkdir(exist_ok=True)
    if any(x.is_symlink() for x in out.rglob('*')): fail('symlink_in_output')
    lockpath = contained(out,out/'.build.lock')
    with lockpath.open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: fail('timeline_build_in_progress')
        manifest = contained(out,out/'timeline.json')
        if manifest.is_file():
            cached = json.loads(manifest.read_text(encoding='utf-8'))
            if cached.get('video_sha256') != digest: fail('source_changed_use_new_evidence_directory')
            if cached.get('version') == VERSION and cached.get('status') in ['aligned_sampled_evidence','visual_only_no_audio']:
                if all(contained(out,Path(x['file'])).is_file() for x in cached['sheets']+cached['frames']):
                    return cached
        segments, asr_status, warnings, asr_source = get_transcript(run,out,video,info,asr_runner)
        frames = extract_frames(video,out,info['duration_sec'])
        sheets = create_sheets(frames,out)
        rows = align_segments(segments,frames)
        sampled_to_end = frames[-1]['time_sec'] >= info['duration_sec']-0.3
        if not sampled_to_end: warnings.append('frame_sampling_truncated_or_missing_tail')
        status = 'aligned_sampled_evidence' if asr_status=='timestamped_asr' and sampled_to_end else 'partial_evidence'
        if asr_status=='no_audio' and sampled_to_end: status='visual_only_no_audio'
        source_file = contained(run,run/'source.url')
        source_url = source_file.read_text(encoding='utf-8').strip()[:2048] if source_file.is_file() else ''
        result={'version':VERSION,'status':status,'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                'evidence_dir':str(run),'timeline_file':str(manifest),'source_url':source_url,'video_sha256':digest,
                **info,'asr_status':asr_status,'asr_source':asr_source,'segments':rows,'frames':frames,'sheets':sheets,
                'sampling':{'hook_interval_sec':0.5,'later_max_interval_sec':2,'scene_threshold':0.30,
                            'frame_count':len(frames),'page_count':len(sheets),'sampled_to_end':sampled_to_end,
                            'scene_boundaries':'algorithm_candidates_not_human_verified'},
                'warnings':warnings,
                'limitations':['机器ASR时间码未经人工听校，文字可能有错词；不是逐字对齐。',
                               '图片是带真实PTS的采样帧，不是全片逐帧观看；切点是算法候选。',
                               '工具不产生视觉语义，模型必须实际读取图片再判断画面。',
                               '未分析音色、音乐和音效，不得据此声称听过原声。']}
        write_transcript(rows,out)
        atomic_json(manifest,result)
        md=['# 声画对齐证据（机器时间码 + 真实采样帧）',f"状态：{status}；时长：{info['duration_sec']:.3f}s；图片页：{len(sheets)}。",'']
        for row in rows:
            md += [f"## {row['id']}｜{row['start_sec']:.3f}—{row['end_sec']:.3f}s",row['text'],f"区间内画面：{', '.join(row['frame_ids']) or '无采样帧'}；边界参考：{row['context_before']} / {row['context_after']}",'']
        md += ['## 读取限制']+result['limitations']
        (out/'timeline.md').write_text('\n'.join(md),encoding='utf-8')
        return result

def summarize(d):
    return {k:d[k] for k in ['version','status','evidence_dir','timeline_file','source_url','duration_sec','asr_status','sampling','warnings','limitations']}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('evidence_dir');args=parser.parse_args()
    try: print(json.dumps(summarize(build_timeline(args.evidence_dir)),ensure_ascii=False))
    except Exception as e:
        message=str(e) if isinstance(e,ValueError) else type(e).__name__
        print(json.dumps({'ok':False,'error':message},ensure_ascii=False));return 1
    return 0
if __name__=='__main__':sys.exit(main())
