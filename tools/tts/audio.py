"""edge-tts synthesis (cached per line) and ffmpeg assembly of per-line files and full.mp3."""
import asyncio
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

LINE_FILE_RE = re.compile(r'^L\d{4}-(god|narrator|p(0[1-9]|1[0-2]))\.mp3$')
RATE_RE = re.compile(r'^[+-]\d{1,3}%$')
PITCH_RE = re.compile(r'^[+-]\d{1,3}Hz$')


class AudioError(RuntimeError):
    pass


def voice_params(voices, speaker):
    v = voices['speakers'][speaker]
    params = {'voice': v['voice'], 'rate': v.get('rate', '+0%'), 'pitch': v.get('pitch', '+0Hz'),
              'volume': v.get('volume', '+0%')}
    if not (RATE_RE.match(params['rate']) and RATE_RE.match(params['volume']) and PITCH_RE.match(params['pitch'])):
        raise AudioError(f'voices.json 中 {speaker} 的 rate/pitch/volume 格式无效：{params}')
    return params


def cache_key(params, text):
    return hashlib.sha256(json.dumps([params, text], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def probe(path):
    proc = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AudioError(f'ffprobe 无法解析 {path}: {proc.stderr.strip()[:300]}')
    data = json.loads(proc.stdout)
    stream = next(s for s in data['streams'] if s['codec_type'] == 'audio')
    return {'format': data['format']['format_name'], 'codec': stream['codec_name'],
            'sample_rate': int(stream['sample_rate']), 'channels': int(stream['channels']),
            'duration': float(data['format']['duration'])}


async def _synth_one(edge_tts, text, params, out, semaphore, retries=5):
    async with semaphore:
        last = None
        for attempt in range(retries):
            tmp = out.with_suffix('.part')
            try:
                communicate = edge_tts.Communicate(text, params['voice'], rate=params['rate'],
                                                   pitch=params['pitch'], volume=params['volume'])
                await communicate.save(str(tmp))
                if tmp.stat().st_size < 1000:
                    raise AudioError('返回音频过小')
                os.replace(tmp, out)
                return
            except Exception as exc:  # network errors, NoAudioReceived, ...
                last = exc
                tmp.unlink(missing_ok=True)
                await asyncio.sleep(2 ** attempt)
        raise AudioError(f'edge-tts 合成失败（{params["voice"]}）：{type(last).__name__}: {last}')


def synthesize(jobs, cache_dir, concurrency=3, log=print):
    """jobs: list of (text, params). Returns list of cached mp3 paths (same order)."""
    import edge_tts
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths, todo = [], {}
    for text, params in jobs:
        path = cache_dir / f'{cache_key(params, text)}.mp3'
        paths.append(path)
        if not path.exists():
            todo[path] = (text, params)
    unique = len(set(paths))
    log(f'edge-tts {getattr(edge_tts, "__version__", "?")}：共 {len(jobs)} 句（去重后 {unique} 段），'
        f'已有缓存 {unique - len(todo)}，需合成 {len(todo)}')

    async def run():
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [_synth_one(edge_tts, text, params, path, semaphore) for path, (text, params) in todo.items()]
        done = 0
        for future in asyncio.as_completed(tasks):
            await future
            done += 1
            if done % 20 == 0 or done == len(tasks):
                log(f'  已合成 {done}/{len(tasks)}')

    if todo:
        asyncio.run(run())
    return paths


def decode_pcm(path, sample_rate):
    proc = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 's16le', '-acodec', 'pcm_s16le',
                           '-ac', '1', '-ar', str(sample_rate), '-'], capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise AudioError(f'ffmpeg 解码失败 {path}: {proc.stderr.decode(errors="replace")[:300]}')
    return proc.stdout


def encode_cmd(out, sample_rate, bitrate):
    return ['ffmpeg', '-v', 'error', '-y', '-f', 's16le', '-ar', str(sample_rate), '-ac', '1', '-i', '-',
            '-c:a', 'libmp3lame', '-b:a', bitrate, '-f', 'mp3', str(out)]


def assemble(entries, clips, audio_dir, settings, log=print):
    """Write per-line mp3s + full.mp3. Times come from exact PCM sample counts, so
    every segment starts where the previous one ends (padding belongs to the line)."""
    rate, bitrate = settings['sample_rate'], settings['bitrate']
    pause = int(rate * settings['pause_after_line_ms'] / 1000)
    phase_pause = int(rate * settings['pause_on_phase_change_ms'] / 1000)
    audio_dir = Path(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    full_tmp = audio_dir / 'full.mp3.part'
    full = subprocess.Popen(encode_cmd(full_tmp, rate, bitrate), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    cursor, timeline, names = 0, [], set()
    try:
        for i, (entry, clip) in enumerate(zip(entries, clips)):
            pcm = decode_pcm(clip, rate)
            if len(pcm) % 2:
                pcm = pcm[:-1]
            voice_samples = len(pcm) // 2
            nxt = entries[i + 1] if i + 1 < len(entries) else None
            pad = pause + (phase_pause if nxt is None or nxt['phase_label'] != entry['phase_label'] else 0)
            pcm += b'\x00\x00' * pad
            samples = voice_samples + pad
            name = f"{entry['id']}-{entry['speaker']}.mp3"
            names.add(name)
            tmp = audio_dir / (name + '.part')
            proc = subprocess.run(encode_cmd(tmp, rate, bitrate), input=pcm, capture_output=True)
            if proc.returncode != 0:
                raise AudioError(f'编码 {name} 失败：{proc.stderr.decode(errors="replace")[:300]}')
            os.replace(tmp, audio_dir / name)
            full.stdin.write(pcm)
            start, end = cursor / rate, (cursor + samples) / rate
            timeline.append({'start': round(start, 3), 'end': round(end, 3),
                             'voice_end': round((cursor + voice_samples) / rate, 3),
                             'start_sample': cursor, 'end_sample': cursor + samples,
                             'audio': f'audio/{name}'})
            cursor += samples
            if (i + 1) % 25 == 0:
                log(f'  已拼接 {i + 1}/{len(entries)}')
        full.stdin.close()
        err = full.stderr.read()
        full.stderr.close()
        if full.wait() != 0:
            raise AudioError(f'full.mp3 编码失败：{err.decode(errors="replace")[:300]}')
        os.replace(full_tmp, audio_dir / 'full.mp3')
    except BaseException:
        full.kill()
        full.wait()
        full_tmp.unlink(missing_ok=True)
        raise
    # remove only stale per-line files this tool wrote earlier (strict name pattern)
    stale = [p for p in audio_dir.iterdir() if LINE_FILE_RE.match(p.name) and p.name not in names]
    for p in stale:
        p.unlink()
    return timeline, cursor / rate, len(stale)
