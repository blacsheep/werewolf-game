#!/usr/bin/env python3
"""Check transcript.json / timeline.json / audio of one game and write <game>/tts_verify.json.

Checks: schema and cleaning rules; contiguous, non-overlapping timeline backed by real
files; ffprobe format/duration of every file; random samples cut from full.mp3 match the
per-line file (envelope correlation); every clip's voice is acoustically closest to its
labelled speaker (leave-one-out nearest centroid); optional ASR text comparison.
"""
import argparse
import datetime
import difflib
import json
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio  # noqa: E402
import transcript as T  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parent
VALID_SPEAKERS = {'god', 'narrator', *T.SEATS}
CHANNEL_RE = re.compile(r'^(public|wolves|private:p(0[1-9]|1[0-2]))$')


class Report:
    def __init__(self, log):
        self.checks, self.log = [], log

    def check(self, name, ok, detail=''):
        self.checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
        self.log(f"  [{'通过' if ok else '失败'}] {name}{'：' + str(detail) if detail else ''}")
        return ok


def check_transcript(tr, events, rep):
    entries = tr['entries']
    rep.check('transcript 序号连续且 id 唯一',
              [e['index'] for e in entries] == list(range(1, len(entries) + 1))
              and len({e['id'] for e in entries}) == len(entries), f'{len(entries)} 句')
    required = ('index', 'id', 'speaker', 'speaker_name', 'channel', 'phase', 'phase_label', 'text', 'kind', 'source')
    bad = [e['id'] for e in entries if any(e.get(k) in (None, '') for k in required)]
    rep.check('每句含排序/说话人/频道/阶段/文本', not bad, bad[:5])
    rep.check('说话人与频道合法',
              all(e['speaker'] in VALID_SPEAKERS and CHANNEL_RE.match(e['channel']) for e in entries))
    leftovers = [e['id'] for e in entries if re.search(r'(?<![A-Za-z])p(0[1-9]|1[0-2])(?![0-9])|\*\*|`|^#|@', e['text'])]
    rep.check('文本已清理（无 pXX、Markdown、@）', not leftovers, leftovers[:5])
    acks = [e['id'] for e in entries if T.VOTE_ACK_RE.match(e['text'])]
    rep.check('未录入“已投票”', not acks, acks[:5])
    meta = [e['id'] for e in entries if T.META_RE.search(e['text'])]
    rep.check('未录入主持阻塞/工作汇报等非对局内容', not meta, meta[:5])
    narr_ok = all((e['speaker'] == 'narrator') == bool(e['is_narration']) == (e['kind'] == 'narration') for e in entries)
    rep.check('旁白全部标记为 narrator/is_narration，且非旁白不用旁白声', narr_ok)
    private_ok = all(e['channel'] in ('wolves', f"private:{e['actor']}")
                     for e in entries if e['source'].get('action') in ('guard', 'witch', 'inspect', 'wolf_kill'))
    rep.check('夜间技能旁白在对应私密/狼队频道', private_ok)
    wolf_chat_ok = all(e['channel'] == 'wolves' for e in entries if e['kind'] == 'wolf_chat') and \
        all(e['channel'] == 'public' for e in entries if e['kind'] in ('speech', 'last_words'))
    rep.check('公开发言在 public、狼人讨论在 wolves', wolf_chat_ok)
    speech_events = [e for e in events if e['action'] in ('speak', 'last_words')]
    voiced = {e['source']['seq'] for e in entries if e['source'].get('action') in ('speak', 'last_words')}
    rep.check('事件日志的每条发言/遗言都已录入', voiced == {e['seq'] for e in speech_events},
              f'{len(voiced)}/{len(speech_events)}')
    cov = tr['event_coverage']
    rep.check('每个事件都有覆盖或排除理由', len(cov) == len(events) and '未覆盖' not in cov.values(),
              f'{len(cov)} 个事件')
    keys = [T.epoch(e['at']) for e in entries]
    rep.check('按来源时间戳非递减排序', all(a <= b for a, b in zip(keys, keys[1:])))


def check_chat_sources(tr, config, rep):
    """Every group in tts.json was read, and every chat line keeps the group it was sent in."""
    if config is None:
        rep.check('tts.json 存在（群配置）', False)
        return
    conv = config.get('conversations', {})
    wolves = [conv['wolves']] if isinstance(conv.get('wolves'), str) else list(conv.get('wolves') or [])
    read = {k: v['conversation_id'] for k, v in tr['sources']['chats'].items()}
    rep.check('tts.json 中的每个群都已完整读取并计入来源（公共群 + 全部狼队群）',
              sorted(read.values()) == sorted([conv.get('public')] + wolves)
              and all(T.chat_channel(k) == ('public' if c == conv.get('public') else 'wolves') for k, c in read.items()),
              {k: f"{c}（{tr['sources']['chats'][k]['messages']} 条，录入 {tr['sources']['chats'][k]['included']}）"
               for k, c in read.items()})
    bad = [e['id'] for e in tr['entries'] if e['source']['type'] == 'chat'
           and not (e['source'].get('chat') in read and e['source'].get('conversation_id') == read[e['source']['chat']]
                    and T.chat_channel(e['source']['chat']) == e['channel'])]
    rep.check('每条群消息的来源群 ID、频道与读取来源一致', not bad, bad[:5])


def check_timeline(game_dir, tr, tl, rep):
    rows, entries = tl['entries'], tr['entries']
    rep.check('timeline 与 transcript 一一对应（id/说话人/文本）',
              len(rows) == len(entries) and all(r['id'] == e['id'] and r['speaker'] == e['speaker']
                                                and r['text'] == e['text'] and r['channel'] == e['channel']
                                                and r['phase'] == e['phase'] for r, e in zip(rows, entries)),
              f'{len(rows)} 条')
    contiguous = rows[0]['start'] == 0 and all(a['end'] == b['start'] for a, b in zip(rows, rows[1:])) \
        and all(a['end_sample'] == b['start_sample'] for a, b in zip(rows, rows[1:]))
    rep.check('时间线从 0 开始、首尾相接无缝隙', contiguous)
    rep.check('无重叠且每条时长为正', all(r['end'] > r['start'] and r['start'] <= r['voice_end'] <= r['end'] for r in rows))
    rep.check('总时长等于最后一条结束', abs(rows[-1]['end'] - tl['duration']) < 1e-6, f"{tl['duration']} 秒")
    voices = json.loads((TOOL_DIR / 'voices.json').read_text())
    rep.check('每条音色与 voices.json 一致',
              all(r['voice'] == audio.voice_params(voices, r['speaker']) for r in rows))
    missing = [r['audio'] for r in rows if not (game_dir / r['audio']).is_file()]
    rep.check('每条都有实际存在的分句音频', not missing, missing[:5])
    if missing:
        return {}
    probes, bad, worst = {}, [], 0
    for r in rows:
        p = audio.probe(game_dir / r['audio'])
        probes[r['id']] = p
        diff = abs(p['duration'] - r['duration'])
        worst = max(worst, diff)
        if p['codec'] != 'mp3' or p['sample_rate'] != tl['sample_rate'] or p['channels'] != 1 or diff > 0.08:
            bad.append((r['id'], p, r['duration']))
    rep.check('分句文件 ffprobe：mp3/采样率/单声道，时长与时间线偏差 ≤ 0.08 秒', not bad,
              f'最大偏差 {worst:.3f} 秒' + (f'；异常 {bad[:3]}' if bad else ''))
    full = audio.probe(game_dir / 'audio' / 'full.mp3')
    rep.check('full.mp3 ffprobe 时长与时间线总长一致（≤ 0.2 秒）',
              full['codec'] == 'mp3' and abs(full['duration'] - tl['duration']) <= 0.2,
              f"full.mp3 {full['duration']:.3f} 秒 / 时间线 {tl['duration']:.3f} 秒")
    return probes


# ---------------------------------------------------------------- signal helpers (numpy)

def pcm_array(data):
    import numpy as np
    return np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0


def decode_window(path, start, duration, rate):
    proc = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-ss', f'{start:.3f}', '-t', f'{duration:.3f}',
                           '-f', 's16le', '-ac', '1', '-ar', str(rate), '-'], capture_output=True)
    if proc.returncode != 0:
        raise audio.AudioError(proc.stderr.decode(errors='replace')[:300])
    return pcm_array(proc.stdout)


def envelope(x, rate, frame_ms=10):
    import numpy as np
    n = int(rate * frame_ms / 1000)
    m = len(x) // n
    return np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)


def best_lag(a, b, max_lag=10):
    import numpy as np
    best = (-2.0, 0)
    for lag in range(-max_lag, max_lag + 1):
        x, y = (a[lag:], b[:len(b) - lag]) if lag >= 0 else (a[:lag], b[-lag:])
        k = min(len(x), len(y))
        if k < 20:
            continue
        r = float(np.corrcoef(np.log(x[:k]), np.log(y[:k]))[0, 1])
        best = max(best, (r, lag))
    return best


REFERENCE_SENTENCES = [
    '各位玩家好，现在我来说一下我对今天局势的整体判断，希望大家认真听完。',
    '昨晚的信息比较少，我先不急着下结论，等后面几位发言之后再明确站边。',
    '如果今天必须归票，我会优先考虑发言前后矛盾、票型又对不上的那个位置。',
]


def median_f0(frames, rate):
    import numpy as np
    n_fft = frames.shape[1]
    ac = np.fft.irfft(np.abs(np.fft.rfft(frames, 2 * n_fft, axis=1)) ** 2, axis=1)[:, :n_fft]
    lo, hi = rate // 400, rate // 70
    peak = ac[:, lo:hi].argmax(axis=1) + lo
    strength = ac[np.arange(len(ac)), peak] / (ac[:, 0] + 1e-10)
    f0 = rate / peak[strength > 0.3]
    return float(np.median(f0)) if len(f0) else 0.0


def voice_features(x, rate, seconds=20):
    """Cepstral mean/std over voiced frames (first 20 s of speech) + median F0."""
    import numpy as np
    n_fft, hop = 1024, 256
    loud = np.flatnonzero(np.abs(x) > 0.01)
    if len(loud):
        x = x[loud[0]:loud[-1]]
    x = x[:rate * seconds]
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop] * np.hanning(n_fft)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    energy = power.sum(axis=1)
    voiced = energy > np.percentile(energy, 40)
    freqs = np.fft.rfftfreq(n_fft, 1 / rate)
    mel = lambda f: 2595 * np.log10(1 + f / 700)  # noqa: E731
    edges = 700 * (10 ** (np.linspace(mel(60), mel(11000), 42) / 2595) - 1)
    bank = np.array([np.clip(np.minimum((freqs - lo) / (mid - lo), (hi - freqs) / (hi - mid)), 0, None)
                     for lo, mid, hi in zip(edges, edges[1:], edges[2:])])
    logmel = np.log(power[voiced] @ bank.T + 1e-10)
    logmel -= logmel.mean(axis=1, keepdims=True)
    k = np.arange(40)
    cep = logmel @ np.cos(np.pi / 40 * (k[:, None] + 0.5) * np.arange(1, 20)[None, :])
    f0 = median_f0(frames[voiced], rate)
    return np.concatenate([cep.mean(axis=0), cep.std(axis=0), [np.log2(max(f0, 50.0)) * 6]]), f0


def speaker_check(game_dir, rows, rate, rep, log):
    """Nearest centroid: each clip vs (other clips of a speaker + 3 enrollment sentences
    synthesised from voices.json). Enrollment covers speakers with a single line."""
    import numpy as np
    voices = json.loads((TOOL_DIR / 'voices.json').read_text())
    speakers = list(voices['speakers'])
    jobs = [(t, audio.voice_params(voices, s)) for s in speakers for t in REFERENCE_SENTENCES]
    refs = audio.synthesize(jobs, game_dir / '.tts' / 'voice-ref', log=log)
    ref_feats = {s: [] for s in speakers}
    for (text, _), path, s in zip(jobs, refs, [s for s in speakers for _ in REFERENCE_SENTENCES]):
        ref_feats[s].append(voice_features(pcm_array(audio.decode_pcm(path, rate)), rate)[0])
    feats, f0s = [], []
    for r in rows:
        f, f0 = voice_features(pcm_array(audio.decode_pcm(game_dir / r['audio'], rate)), rate)
        feats.append(f)
        f0s.append(f0)
    feats = np.array(feats)
    every = np.vstack([feats] + [np.array(v) for v in ref_feats.values()])
    mu, sd = every.mean(axis=0), every.std(axis=0) + 1e-9
    z = (feats - mu) / sd
    zref = {s: (np.array(v) - mu) / sd for s, v in ref_feats.items()}
    labels = [r['speaker'] for r in rows]
    predicted = []
    for i in range(len(rows)):
        best = None
        for s in speakers:
            pool = [z[j] for j, l in enumerate(labels) if l == s and j != i] + list(zref[s])
            d = float(np.linalg.norm(z[i] - np.mean(pool, axis=0)))
            best = min(best, (d, s)) if best else (d, s)
        predicted.append(best[1])
    wrong = [(rows[i]['id'], labels[i], predicted[i]) for i in range(len(rows)) if predicted[i] != labels[i]]
    rep.check('全部分句的声学特征最接近其标注说话人（留一法 + 注册参考句最近质心）', not wrong,
              f'{len(rows) - len(wrong)}/{len(rows)} 正确' + (f'；不一致 {wrong[:8]}' if wrong else ''))
    per = {s: round(float(np.median([f for f, l in zip(f0s, labels) if l == s])), 1) for s in sorted(set(labels))}
    log(f'  各说话人基频中位数（Hz）：{per}')
    return predicted, f0s, per


def log_mel(x, rate, n_fft=1024, hop=240):
    import numpy as np
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop] * np.hanning(n_fft)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / rate)
    mel = lambda f: 2595 * np.log10(1 + f / 700)  # noqa: E731
    edges = 700 * (10 ** (np.linspace(mel(60), mel(11000), 42) / 2595) - 1)
    bank = np.array([np.clip(np.minimum((freqs - lo) / (mid - lo), (hi - freqs) / (hi - mid)), 0, None)
                     for lo, mid, hi in zip(edges, edges[1:], edges[2:])])
    return np.log(power @ bank.T + 1e-8)


def spectrogram_similarity(a, b, rate, max_lag=10):
    """Best Pearson r of two log-mel spectrograms over +-100 ms lags, ignoring silent frames.
    Same voice + same text gives ~0.99; a different voice saying the same text stays far lower."""
    import numpy as np
    A, B = log_mel(a, rate), log_mel(b, rate)
    floor, best = np.log(1e-4), -2.0
    for lag in range(-max_lag, max_lag + 1):
        x, y = (A[lag:], B[:len(B) - lag]) if lag >= 0 else (A[:lag], B[-lag:])
        k = min(len(x), len(y))
        x, y = x[:k], y[:k]
        keep = (x.max(axis=1) > floor) | (y.max(axis=1) > floor)
        if keep.sum() >= 20:
            best = max(best, float(np.corrcoef(x[keep].ravel(), y[keep].ravel())[0, 1]))
    return best


def resynth_check(game_dir, picks, texts, f0_by_speaker, rate):
    """Fresh edge-tts requests (bypassing the cache) of each sampled line with its labelled
    voice and the two most confusable voices (same base voice first, then closest median F0);
    the shipped clip must match the labelled voice clearly best."""
    voices = json.loads((TOOL_DIR / 'voices.json').read_text())
    work = game_dir / '.tts' / 'verify-resynth'
    if work.exists():
        for old in work.glob('*.mp3'):
            old.unlink()
    out = []
    for r in picks:
        others = [s for s in voices['speakers'] if s != r['speaker']]
        own_f0 = f0_by_speaker.get(r['speaker'], 0)
        rivals = sorted(others, key=lambda s: (voices['speakers'][s]['voice'] != r['voice']['voice'],
                                               abs(f0_by_speaker.get(s, 0) - own_f0)))[:2]
        text = texts[r['id']]
        candidates = [r['speaker']] + rivals
        jobs = [(text, audio.voice_params(voices, s)) for s in candidates]
        paths = audio.synthesize(jobs, work, log=lambda *_: None)
        clip = pcm_array(audio.decode_pcm(game_dir / r['audio'], rate))
        scores = {}
        for s, path in zip(candidates, paths):
            fresh = pcm_array(audio.decode_pcm(path, rate))
            n = min(len(fresh), len(clip))
            scores[s] = round(spectrogram_similarity(clip[:n], fresh[:n], rate), 4)
        out.append({'id': r['id'], 'labelled': r['speaker'], 'scores': scores,
                    'ok': scores[r['speaker']] >= 0.95 and all(scores[r['speaker']] > scores[s] + 0.05 for s in rivals)})
    return out


def normalize_for_compare(text):
    text = re.sub(r'(?<![0-9])(1[0-2]|[1-9])号', lambda m: T.cn_number(int(m.group(1))) + '号', text)
    return re.sub(r'[\W_]+', '', text)


def run_asr(paths, model_dir, log):
    cmd = ['uv', 'run', '--no-project', '--with', 'faster-whisper', 'python3', str(TOOL_DIR / 'asr.py')]
    if model_dir:
        cmd += ['--model-dir', model_dir]
    proc = subprocess.run(cmd + [str(p) for p in paths], capture_output=True, text=True)
    if proc.returncode != 0:
        log(f'  ASR 不可用：{proc.stderr.strip()[-400:]}')
        return None
    return json.loads(proc.stdout)


def run(game_dir, samples=8, seed=None, asr=False, asr_model_dir=None, log=print):
    game_dir = Path(game_dir)
    rep = Report(log)
    log('核验 transcript.json ...')
    tr = json.loads((game_dir / 'transcript.json').read_text())
    events = [json.loads(l) for l in (game_dir / 'events.jsonl').read_text().splitlines() if l.strip()]
    check_transcript(tr, events, rep)
    config_path = game_dir / 'tts.json'
    check_chat_sources(tr, json.loads(config_path.read_text()) if config_path.exists() else None, rep)
    log('核验 timeline.json 与音频文件 ...')
    tl = json.loads((game_dir / 'timeline.json').read_text())
    probes = check_timeline(game_dir, tr, tl, rep)
    rows, rate = tl['entries'], tl['sample_rate']
    sample_report = []
    if probes:
        log('声学说话人自洽检查（全部分句）...')
        predicted, f0s, f0_by_speaker = speaker_check(game_dir, rows, rate, rep, log)
        seed = seed if seed is not None else random.SystemRandom().randrange(10 ** 6)
        rng = random.Random(seed)
        # stratify: always include a narration, a host line, a wolf chat line and player speeches
        pools = [[r for r in rows if r['kind'] == k] for k in ('narration', 'host', 'wolf_chat')]
        picks = [rng.choice(p) for p in pools if p]
        rest = [r for r in rows if r not in picks]
        picks += rng.sample(rest, max(0, min(samples - len(picks), len(rest))))
        picks.sort(key=lambda r: r['index'])
        log(f'随机抽检 {len(picks)} 句（种子 {seed}）...')
        texts = {e['id']: e['tts_text'] for e in tr['entries']}
        resynth = {x['id']: x for x in resynth_check(game_dir, picks, texts, f0_by_speaker, rate)}
        asr_out = run_asr([game_dir / r['audio'] for r in picks], asr_model_dir, log) if asr else None
        aligned_all = True
        for k, r in enumerate(picks):
            i = r['index'] - 1
            clip = pcm_array(audio.decode_pcm(game_dir / r['audio'], rate))
            window = decode_window(game_dir / 'audio' / 'full.mp3', r['start'], r['duration'], rate)
            corr, lag = best_lag(envelope(clip, rate), envelope(window, rate))
            aligned = corr >= 0.9 and abs(lag) <= 3
            aligned_all &= aligned
            item = {'id': r['id'], 'speaker': r['speaker'], 'channel': r['channel'], 'phase_label': r['phase_label'],
                    'start': r['start'], 'end': r['end'], 'audio': r['audio'], 'voice': r['voice']['voice'],
                    'rate': r['voice']['rate'], 'pitch': r['voice']['pitch'],
                    'ffprobe_duration': probes[r['id']]['duration'],
                    'full_vs_clip_corr': round(corr, 4), 'full_vs_clip_lag_ms': lag * 10,
                    'acoustic_nearest_speaker': predicted[i], 'median_f0_hz': round(f0s[i], 1),
                    'resynth_scores': resynth[r['id']]['scores'], 'resynth_ok': resynth[r['id']]['ok'],
                    'text': r['text'][:60]}
            if asr_out:
                heard = asr_out[k]['text']
                ratio = difflib.SequenceMatcher(None, normalize_for_compare(heard),
                                                normalize_for_compare(r['text'])).ratio()
                item.update(asr_text=heard[:80], asr_similarity=round(ratio, 3))
            sample_report.append(item)
            log(f"  {r['id']} {r['speaker']} {r['voice']['voice']} {r['start']:.1f}-{r['end']:.1f}s "
                f"对齐r={corr:.3f} 偏移={lag * 10}ms 声学最近={predicted[i]} 重合成={resynth[r['id']]['scores']}"
                + (f" ASR相似度={item['asr_similarity']}" if 'asr_similarity' in item else '')
                + f" | {r['text'][:24]}")
        rep.check('抽检：从 full.mp3 按时间线截取的片段与分句文件一致（r ≥ 0.9，偏移 ≤ 30ms）', aligned_all)
        rep.check('抽检：声学最近说话人与标注一致',
                  all(s['acoustic_nearest_speaker'] == s['speaker'] for s in sample_report))
        rep.check('抽检：重新请求 edge-tts 合成同一整句，分句文件的频谱图只与标注音色吻合（r ≥ 0.95 且高出对照音色 0.05 以上）',
                  all(s['resynth_ok'] for s in sample_report))
        if asr_out:
            rep.check('抽检：ASR 识别文本与 transcript 相似度 ≥ 0.75',
                      all(s['asr_similarity'] >= 0.75 for s in sample_report),
                      [s['asr_similarity'] for s in sample_report])
    ok = all(c['ok'] for c in rep.checks)
    result = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
              'ok': ok, 'seed': seed, 'checks': rep.checks, 'samples': sample_report,
              'human_listening': '未进行人工试听；以上为自动化探测结果',
              'asr': 'faster-whisper small (int8, CPU)' if asr else '未启用'}
    (game_dir / 'tts_verify.json').write_text(json.dumps(result, ensure_ascii=False, indent=1) + '\n')
    log(f"核验{'全部通过' if ok else '存在失败项'}，详见 {game_dir / 'tts_verify.json'}")
    return ok


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', default=str(TOOL_DIR.parents[1] / 'games' / 'current'))
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--asr', action='store_true')
    parser.add_argument('--asr-model-dir')
    a = parser.parse_args()
    sys.exit(0 if run(a.game, a.samples, a.seed, a.asr, a.asr_model_dir) else 1)
