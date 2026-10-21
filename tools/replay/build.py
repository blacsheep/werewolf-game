#!/usr/bin/env python3
"""Build a local replay from immutable game inputs."""
import argparse
import bisect
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

TOOL = Path(__file__).resolve().parent
ROOT = TOOL.parents[1]
ROLES = {'wolf': '狼人', 'villager': '村民', 'seer': '预言家', 'witch': '女巫', 'hunter': '猎人', 'guard': '守卫'}
INPUTS = ['timeline.json', 'transcript.json', 'seats.json', 'events.jsonl', 'audio/full.mp3']


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def hashes(game):
    return {name: hashlib.sha256((game / name).read_bytes()).hexdigest() for name in INPUTS}


def probe(path):
    run = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(run.stdout)


def check_memory(required=512_000_000):
    """Allow reclaimable inactive file cache; respect ancestor cgroup limits."""
    mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(mem['MemAvailable'].split()[0]) * 1024
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        if not line.startswith('0::'):
            continue
        root = Path('/sys/fs/cgroup')
        group = root / line[3:].lstrip('/')
        while group != root:
            if (group / 'memory.max').exists():
                limit = (group / 'memory.max').read_text().strip()
                if limit != 'max':
                    stats = dict(row.split() for row in (group / 'memory.stat').read_text().splitlines())
                    current = int((group / 'memory.current').read_text())
                    available = min(available, int(limit) - current + int(stats.get('inactive_file', 0)))
            group = group.parent
    if available < required:
        raise RuntimeError(f'可用内存不足：估计 {available/1024**2:.0f}MiB，需要 {required/1024**2:.0f}MiB；安全停止')
    return available


def check_entries(entries):
    previous = 0
    ids = set()
    for e in entries:
        if e['id'] in ids or not all(math.isfinite(e[k]) for k in ('start', 'end')):
            raise ValueError('重复条目或非有限时间')
        if abs(e['start'] - previous) > .0001 or e['end'] <= e['start']:
            raise ValueError(f"时间轴重叠/缺口：{e['id']}")
        if e['channel'] not in ('public', 'wolves') and not e['channel'].startswith('private:p'):
            raise ValueError('未知频道')
        ids.add(e['id'])
        previous = e['end']
    if not entries:
        raise ValueError('空时间轴')


def load_game(game):
    timeline, transcript, seats = (read(game / p) for p in INPUTS[:3])
    entries = timeline['entries']
    check_entries(entries)
    sources = {e['id']: e for e in transcript['entries']}
    for e in entries:
        t = sources[e['id']]
        if any(e[k] != t[k] for k in ('text', 'speaker', 'channel', 'phase_label')):
            raise ValueError(f"transcript 映射不匹配：{e['id']}")
    spec = importlib.util.spec_from_file_location('replay_engine', ROOT / 'engine/server.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    fd = os.open(game, os.O_RDONLY | os.O_DIRECTORY)
    try:
        final, events, _ = engine.Store(game.parent).load(fd)
    finally:
        os.close(fd)
    if not final.winner:
        raise ValueError('仅支持已结束的完整对局')
    by_seq = {}
    for i, e in enumerate(entries):
        source = sources[e['id']]['source']
        if source['type'] == 'chat' and 'announces' in source:
            by_seq.setdefault(source['announces'], []).append(i)
            continue
        if source['type'] not in ('event', 'engine_announcement'):
            continue
        seqs = source.get('seq', [])
        for seq in seqs if isinstance(seqs, list) else [seqs]:
            by_seq.setdefault(seq, []).append(i)
    changes = {}
    state = engine.Game()
    alive, sheriff, winner = set(engine.SEATS), None, None
    for event in events:
        state.apply(event['actor'], event['action'], event['args'])
        candidates = by_seq.get(event['seq'], [])

        def locate(kinds):
            for i in candidates:
                if sources[entries[i]['id']]['source'].get('kind') in kinds:
                    return i
            for i in candidates:
                if sources[entries[i]['id']]['source']['type'] in ('event', 'chat'):
                    return i
            raise ValueError(f"状态变化缺少公告映射：seq={event['seq']}, {kinds}")

        if alive != state.alive:
            i = locate({'night_deaths', 'exiled'})
            changes.setdefault(i, {})['alive'] = sorted(state.alive)
        if sheriff != state.sheriff:
            i = locate({'sheriff_elected', 'badge_lost'})
            changes.setdefault(i, {})['sheriff'] = state.sheriff
        if winner != state.winner:
            i = locate({'game_over'})
            changes.setdefault(i, {})['winner'] = state.winner
        alive, sheriff, winner = set(state.alive), state.sheriff, state.winner
    races = sheriff_races(engine, events)
    current = {'alive': list(engine.SEATS), 'sheriff': None, 'winner': None}
    result, public_time = [], 0
    for i, e in enumerate(entries):
        current = {**current, **changes.get(i, {})}
        row = {k: e[k] for k in ('id', 'start', 'end', 'speaker', 'speaker_name', 'channel', 'phase_label', 'text')}
        row['voice_end'] = e.get('voice_end', e['end'])
        row['state'] = {**current, 'candidates': race_at(races, sources[e['id']]['at'])}
        if current['winner']:
            row['phase_label'] = f"第{e['day']}天 · 游戏结束"
        row['public_start'] = round(public_time, 6) if e['channel'] == 'public' else None
        if e['channel'] == 'public':
            public_time += e['end'] - e['start']
        row['public_end'] = round(public_time, 6) if e['channel'] == 'public' else None
        result.append(row)
    model_seats = seats['seats']
    players = [{'id': p, 'number': n, 'role': ROLES[final.roles[p]],
                'model': model_seats.get(p, {}).get('model') or seats.get('all_seats_model') or '未知',
                'provider': model_seats.get(p, {}).get('provider') or seats.get('all_seats_provider') or '未知'}
               for n, p in enumerate(engine.SEATS, 1)]
    for p in players:
        p['model_label'] = p['model'] if p['provider'] == '未知' else f"{p['model']} · {p['provider']}"
    return {'game_id': timeline['game_id'], 'entries': result, 'players': players,
            'duration': entries[-1]['end'], 'public_duration': round(public_time, 6)}


def epoch(at):
    return datetime.datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()


def sheriff_races(engine, events):
    """(time, seats still running for sheriff) after every event."""
    game, result = engine.Game(), []
    for event in events:
        game.apply(event['actor'], event['action'], event['args'])
        vote = getattr(game, 'vote_state', None) or {}
        running = game.phase in ('sheriff_speech', 'withdraw', 'sheriff_pk_speech') or (
            game.phase in ('vote', 'vote_result') and vote.get('kind') == 'sheriff')
        race = [p for p in getattr(game, 'candidates', []) if not getattr(game, 'withdrawals', {}).get(p)]
        result.append((epoch(event['at']), race if running else []))
    return result


def race_at(races, at):
    """Candidates in the race when a line was said."""
    i = bisect.bisect_right([t for t, _ in races], epoch(at)) - 1
    return races[i][1] if i >= 0 else []


def public_ranges(entries):
    ranges = []
    for e in entries:
        if e['channel'] != 'public':
            continue
        if ranges and abs(ranges[-1][1] - e['start']) < .000001:
            ranges[-1][1] = e['end']
        else:
            ranges.append([e['start'], e['end']])
    return ranges


def public_audio(game, data, temp):
    # Decode once to disk, then stream only public sample ranges to the encoder.
    # A concat filter with one branch per sentence buffers minutes of audio per branch.
    source = game / 'audio/full.mp3'
    stream = next(s for s in probe(source)['streams'] if s['codec_type'] == 'audio')
    rate, channels = int(stream['sample_rate']), int(stream['channels'])
    raw = temp / 'source.pcm'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', str(source),
                    '-f', 's16le', '-c:a', 'pcm_s16le', str(raw)], check=True)
    ranges = public_ranges(data['entries'])
    with subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 's16le', '-ar', str(rate), '-ac', str(channels), '-i', 'pipe:0',
            '-c:a', 'libmp3lame', '-b:a', '48k', str(temp / 'public.mp3')], stdin=subprocess.PIPE) as encoder:
        try:
            with raw.open('rb') as pcm:
                for start, end in ranges:
                    pcm.seek(round(start * rate) * channels * 2)
                    remaining = (round(end * rate) - round(start * rate)) * channels * 2
                    while remaining:
                        chunk = pcm.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError('源音频采样不足，公开音轨未完成')
                        encoder.stdin.write(chunk)
                        remaining -= len(chunk)
        finally:
            encoder.stdin.close()
        if encoder.wait():
            raise RuntimeError('公开音轨编码失败')
    raw.unlink()
    actual = float(probe(temp / 'public.mp3')['format']['duration'])
    if abs(actual - data['public_duration']) > .1:
        raise ValueError(f'公开音轨时长不合格：{actual}')
    return {'duration': actual, 'expected_duration': data['public_duration'],
            'delta_seconds': round(actual - data['public_duration'], 6), 'ranges': ranges,
            'ffmpeg_exit_code': 0, 'ffprobe_exit_code': 0}


def frame_cues(data):
    """(entry, subtitle page) for every video frame, in time order."""
    from render import cues
    return [(e, cue) for e in data['entries'] for cue in cues(e)]


def encode_video(game, data, temp, audio_duration):
    from render import render_frame
    check_memory()
    manifest = []
    pages = frame_cues(data)
    for i, (e, cue) in enumerate(pages):
        frame = temp / f'frame-{i:04}.png'
        rendered = render_frame(data, e, frame, cue)
        end = pages[i + 1][1]['start'] if i + 1 < len(pages) else audio_duration
        manifest.append({'id': e['id'], 'page': cue['index'], 'pages': cue['count'], 'start': cue['start'], 'end': end,
                         'image_sha256': hashlib.sha256(frame.read_bytes()).hexdigest(),
                         'subtitle_sha256': hashlib.sha256(cue['text'].encode()).hexdigest(),
                         'speaker': e['speaker'], 'phase': e['phase_label'], 'state': e['state'],
                         'rendered': rendered})
        if (i + 1) % 50 == 0:
            print(f'已渲染 {i + 1}/{len(pages)} 张字幕画面', flush=True)
    concat = ['ffconcat version 1.0']
    for i, e in enumerate(manifest):
        duration = e['end'] - e['start'] - (.001 if i == len(manifest) - 1 else 0)
        concat += [f"file 'frame-{i:04}.png'", 'option framerate 1000', f'duration {duration:.6f}']
    concat += [f"file 'frame-{len(manifest)-1:04}.png'", 'option framerate 1000']
    (temp / 'frames.txt').write_text('\n'.join(concat) + '\n')
    check_memory()
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1', '-filter_threads', '1', '-safe', '0', '-f', 'concat',
        '-i', str(temp / 'frames.txt'), '-i', str(game / 'audio/full.mp3'), '-map', '0:v', '-map', '1:a',
        '-c:v', 'libx264', '-threads', '1', '-x264-params', 'rc-lookahead=10', '-preset', 'veryfast', '-tune', 'stillimage', '-crf', '24',
        '-pix_fmt', 'yuv420p', '-bf', '0', '-fps_mode', 'vfr', '-enc_time_base', '1:1000',
        '-video_track_timescale', '1000', '-af', 'apad', '-c:a', 'aac', '-b:a', '64k', '-t', f'{audio_duration:.6f}',
        '-movflags', '+faststart', str(temp / 'god.mp4')]
    subprocess.run(cmd, check=True)
    write(temp / 'frames.json', manifest)
    return cmd


def verify_frame_images(temp, count):
    """Decode sequentially; sparse-frame VBV can preserve stale subtitle blocks."""
    from PIL import Image, ImageChops, ImageStat
    from render import CAPTION_BOX
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1',
           '-filter_threads', '1', '-i', str(temp / 'god.mp4'), '-map', '0:v',
           '-fps_mode', 'passthrough', '-enc_time_base', '1:1000', '-pix_fmt', 'rgb24',
           '-threads', '1', '-f', 'rawvideo', 'pipe:1']
    maxima = {'full': 0, 'caption': 0}
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as decoder:
        try:
            for i in range(count + 1):
                raw = decoder.stdout.read(1280 * 720 * 3)
                if len(raw) != 1280 * 720 * 3:
                    raise ValueError(f'解码帧不完整：{i}')
                with Image.frombytes('RGB', (1280, 720), raw) as actual, Image.open(temp / f'frame-{min(i, count-1):04}.png') as expected:
                    with ImageChops.difference(actual, expected) as diff:
                        full = sum(ImageStat.Stat(diff).mean) / 3
                        with diff.crop(CAPTION_BOX) as caption:
                            error = sum(ImageStat.Stat(caption).mean) / 3
                    maxima['full'] = max(maxima['full'], full)
                    maxima['caption'] = max(maxima['caption'], error)
                    if full > 3 or error > 3:
                        raise ValueError(f'视频帧与源图差异过大：{i}, full={full:.3f}, caption={error:.3f}')
            if decoder.stdout.read(1):
                raise ValueError('视频包含多余帧')
            if decoder.wait():
                raise RuntimeError('视频解码失败')
        finally:
            decoder.stdout.close()
            if decoder.poll() is None:
                decoder.terminate()
                decoder.wait()
    return {'frames_compared': count + 1, 'max_mean_absolute_error': maxima,
            'tolerance_8bit': 3, 'ffmpeg_exit_code': 0}


def verify_media(video, audio_duration, data):
    media = probe(video)
    streams = {s['codec_type']: float(s['duration']) for s in media['streams']}
    if set(streams) != {'audio', 'video'}:
        raise ValueError('MP4 必须同时包含音频和视频')
    delta = {k: round(v - audio_duration, 6) for k, v in streams.items()}
    size = video.stat().st_size
    if size >= 300_000_000 or any(abs(v) > .1 for v in delta.values()):
        raise ValueError(f'MP4 大小/时长不合格：{size}, {delta}')
    run = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_frames',
        '-show_entries', 'frame=pts_time,pkt_duration_time', '-of', 'json', str(video)], capture_output=True, text=True, check=True)
    frames = json.loads(run.stdout)['frames']
    expected = [cue['start'] for _, cue in frame_cues(data)] + [round(audio_duration - .001, 3)]
    if len(frames) != len(expected) or any(abs(float(f['pts_time']) - t) > .0011 for f, t in zip(frames, expected)):
        raise ValueError('MP4 实际帧 PTS 与逐句时间不匹配')
    return {'ffprobe_exit_code': 0, 'ffmpeg_exit_code': 0, 'audio_source_duration': audio_duration,
            'timeline_duration': data['duration'], 'streams_duration': streams, 'delta_seconds': delta,
            'size_bytes': size, 'actual_frame_count': len(frames), 'subtitle_frames': len(expected) - 1,
            'frame_pts_verified': True, 'tolerance_seconds': .1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path, default=ROOT / 'games/current')
    parser.add_argument('--web-only', action='store_true', help='仅网页与公开音轨；不表示视频验收完成')
    args = parser.parse_args()
    check_memory()
    if not args.web_only:
        from render import font
        font(20)  # Check Pillow and the Chinese font before generating outputs.
    game = args.game.resolve()
    output = game / 'replay'
    output.mkdir(exist_ok=True)
    free = shutil.disk_usage(output).free
    print(f'剩余空间 {free / 1024**3:.2f} GiB；输出 {output}', flush=True)
    if free < 700_000_000:
        raise RuntimeError('至少需要 700MB 临时可用空间')
    before = hashes(game)
    data = load_game(game)
    audio_duration = float(probe(game / 'audio/full.mp3')['format']['duration'])
    if abs(audio_duration - data['duration']) > .1:
        raise ValueError('timeline 与 full.mp3 时长差超过 0.1 秒')
    with tempfile.TemporaryDirectory(prefix='.build-', dir=TOOL) as directory:
        temp = Path(directory)
        # Temporary HTML uses an absolute audio URI; final HTML keeps relative paths.
        template = (TOOL / 'page.html').read_text()
        encoded = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
        html = template.replace('__REPLAY_DATA__', encoded)
        (temp / 'index.html').write_text(html.replace('../audio/full.mp3', (game / 'audio/full.mp3').as_uri()))
        public_report = public_audio(game, data, temp)
        report = {'input_sha256': before, 'engine_sha256': hashlib.sha256((ROOT / 'engine/server.py').read_bytes()).hexdigest(),
                  'entries': len(data['entries']), 'public_entries': sum(e['channel'] == 'public' for e in data['entries']),
                  'public_audio': public_report}
        outputs = ['public.mp3', 'index.html', 'build-report.json']
        if not args.web_only:
            report['ffmpeg_command'] = encode_video(game, data, temp, audio_duration)
            report['video'] = verify_media(temp / 'god.mp4', audio_duration, data)
            report['frame_images'] = verify_frame_images(temp, len(frame_cues(data)))
            outputs += ['god.mp4', 'frames.json']
        if hashes(game) != before:
            raise RuntimeError('生成期间输入数据发生改变，未发布产物')
        report['inputs_unchanged'] = True
        (temp / 'index.html').write_text(html)
        write(temp / 'build-report.json', report)
        for name in outputs:
            os.replace(temp / name, output / name)
        print(json.dumps(report.get('video', {'web_only': True}), ensure_ascii=False), flush=True)
    print('完成；临时图像已清理。', flush=True)


if __name__ == '__main__':
    main()
