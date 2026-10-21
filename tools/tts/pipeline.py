#!/usr/bin/env python3
"""One command per game: fetch chats -> transcript.json -> edge-tts audio -> timeline.json -> verify.

Usage (from the repo root):
    tools/tts/run.sh                                   # games/current, chat IDs from games/current/tts.json
    tools/tts/run.sh --public <群ID> --wolves <群ID>   # first run of a new game; IDs are saved to tts.json
    tools/tts/run.sh --public <群ID> --wolves <群ID1> <群ID2> ...   # wolves talked in several groups
See tools/tts/README.md.
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio  # noqa: E402
import chats  # noqa: E402
import transcript  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = Path(__file__).resolve().parent
CHANNEL_NAMES = {'public': '公共群', 'wolves': '狼队群'}


def log(msg):
    print(msg, flush=True)


def write_json(path, data, mode=0o644):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write('\n')
    os.replace(tmp, path)


def wolf_ids(value):
    """tts.json keeps one wolf group as a string and several as a list."""
    return [value] if isinstance(value, str) else list(value or [])


def chat_sources(conversations):
    """[(source key, channel, conversation id)]. Several wolf groups are keyed 'wolves@<群ID>'."""
    wolves = wolf_ids(conversations['wolves'])
    if len(set(wolves)) != len(wolves) or conversations['public'] in wolves:
        sys.exit(f'群 ID 重复：public={conversations["public"]} wolves={wolves}')
    keys = ['wolves'] if len(wolves) == 1 else [f'wolves@{c}' for c in wolves]
    return [('public', 'public', conversations['public'])] + [(k, 'wolves', c) for k, c in zip(keys, wolves)]


def load_config(game_dir, args):
    path = game_dir / 'tts.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    conversations = config.setdefault('conversations', {})
    changed = False
    if args.wolves:
        args.wolves = args.wolves[0] if len(args.wolves) == 1 else args.wolves
    for key in ('public', 'wolves'):
        value = getattr(args, key)
        if value and conversations.get(key) != value:
            conversations[key] = value
            changed = True
    config.setdefault('exclude', {})
    config.setdefault('include', [])
    recorded = game_dir / 'conversations.json'
    if not conversations.get('wolves') and recorded.exists():
        # The host records the wolves group with the engine during the game.
        wolves = json.loads(recorded.read_text()).get('wolves')
        if wolves:
            conversations['wolves'] = wolves
            changed = True
    missing = [k for k in ('public', 'wolves') if not conversations.get(k)]
    if missing:
        sys.exit(f'缺少群 ID：{missing}。首次运行请传 --public <ID> --wolves <ID>，会保存到 {path}')
    if changed or not path.exists():
        write_json(path, config)
        log(f'已写入配置 {path}')
    return config


def read_events(game_dir):
    path = game_dir / 'events.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not events:
        sys.exit(f'{path} 为空')
    return events, path


def step_fetch(game_dir, config, offline):
    """Every configured group must be read in full; any failure stops the run (no group is skipped)."""
    work = game_dir / '.tts'
    result = {}
    for key, channel, conversation in chat_sources(config['conversations']):
        name = CHANNEL_NAMES[channel] + (f' {conversation}' if key != channel else '')
        raw = work / f"chat-{key.replace('@', '-')}.json"
        if offline:
            if not raw.exists():
                sys.exit(f'--offline 但没有本地缓存 {raw}')
            messages, info = chats.load_raw(raw)
            if json.loads(raw.read_text()).get('conversation') != conversation:
                sys.exit(f'本地缓存 {raw} 不是群 {conversation} 的')
            log(f'{name}：使用本地缓存 {len(messages)} 条（{info.get("fetched_at")} 拉取）')
        else:
            try:
                messages, info = chats.fetch_all(conversation)
            except chats.FetchError as exc:
                sys.exit(f'{name} 读取失败，停止生成（不会跳过该群）：{exc}')
            info['fetched_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
            chats.save_raw(raw, conversation, messages, info)
            log(f'{name}：拉取 {info["count"]} 条，{info["pages"]} 页，seq {info["first_seq"]}–{info["last_seq"]}，缺号 {info["seq_gaps"]}')
        if info.get('seq_gaps'):
            log(f'  警告：{key} 的消息序号有缺口 {info["seq_gaps"]}')
        for m in messages:      # fetched from this group, so the source group is known
            if m.get('conversation_id') is None:
                m['conversation_id'] = conversation
        result[key] = (conversation, messages, info)
    return result


def step_transcript(game_dir, config, fetched):
    events, events_path = read_events(game_dir)
    seats_path = game_dir / 'seats.json'
    seats = json.loads(seats_path.read_text())['seats'] if seats_path.exists() else {}
    entries, excluded, counts, coverage = transcript.build(
        events, {c: fetched[c][1] for c in fetched}, seats, config)
    uncovered = [s for s, v in coverage.items() if v == '未覆盖']
    if uncovered:
        sys.exit(f'事件未被覆盖：{uncovered}')
    talk = transcript.tech_talk(entries)
    if talk and not config.get('allow_tech_talk'):
        lines = '\n'.join(f"  {e['id']} {e['speaker']}：{e['text'][:80]}" for e in talk)
        sys.exit(f'{len(talk)} 句含工具或引擎用语，不能念给观众听；改写或在 tts.json 的 exclude 中排除后再生成'
                 f'（确需保留可设 allow_tech_talk）：\n{lines}')
    channels = {'public': '公共群', 'wolves': '狼队群'}
    for e in entries:
        if e['channel'].startswith('private:'):
            channels[e['channel']] = f"上帝与{transcript.seat_cn(e['channel'][8:])}的私密频道"
    data = {
        'schema_version': 1,
        'game_id': game_dir.resolve().name,
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'generated_by': 'tools/tts/pipeline.py',
        'sources': {
            'events': {'path': 'events.jsonl', 'count': len(events), 'last_seq': events[-1]['seq'],
                       'last_hash': events[-1]['hash'], 'sha256': transcript.digest(events_path)},
            'chats': {c: {'conversation_id': fetched[c][0], 'channel': transcript.chat_channel(c), **counts[c],
                          'pages': fetched[c][2].get('pages'), 'seq_gaps': fetched[c][2].get('seq_gaps'),
                          'fetched_at': fetched[c][2].get('fetched_at')} for c in fetched},
        },
        'rules': [
            '主线为 events.jsonl：玩家发言、遗言来自 speak/last_words 事件原文；群里逐字相同的玩家消息视为重复，以事件为准。',
            '夜间技能、警长报名/退水、定序、移交警徽、自爆，以及引擎公告（死讯、票型、放逐、胜负）写成旁白：speaker=narrator、is_narration=true，不是任何人实际说过的话。',
            '守卫/女巫/预言家旁白标为 private:座位（上帝与该玩家的私密频道），狼刀旁白标为 wolves；其余旁白为 public。',
            '投票是秘密的，不逐张播报；在引擎公布票型时合并为一条公开旁白（source.ballot_seqs 列出对应投票事件）。',
            '群聊纳入上帝主持词（公共群、狼队群）和狼人夜间讨论；排除“已投票”、主持阻塞/工作汇报等非对局内容（见 excluded）。',
            '清理：pXX 与阿拉伯数字座号改成“几号”，去掉 Markdown 与 @；tts_text 仅把下划线/括号改为空格以便朗读。',
            '排序：按来源时间戳（事件 at、消息 created_at），同时刻按事件序号/消息序号稳定排序；阶段由引擎重放得到。',
        ],
        'speakers': {s: v['name'] for s, v in json.loads((TOOL_DIR / 'voices.json').read_text())['speakers'].items()},
        'channels': channels,
        'entries': entries,
        'excluded': excluded,
        'event_coverage': coverage,
    }
    write_json(game_dir / 'transcript.json', data)
    log(f'transcript.json：{len(entries)} 句（事件 {len(events)} 条；排除群消息 {len(excluded)} 条）')
    return data


def step_audio(game_dir, data, concurrency):
    voices = json.loads((TOOL_DIR / 'voices.json').read_text())
    entries = data['entries']
    unknown = sorted({e['speaker'] for e in entries} - set(voices['speakers']))
    if unknown:
        sys.exit(f'voices.json 缺少说话人：{unknown}')
    params = [audio.voice_params(voices, e['speaker']) for e in entries]
    clips = audio.synthesize([(e['tts_text'], p) for e, p in zip(entries, params)],
                             game_dir / '.tts' / 'cache', concurrency=concurrency, log=log)
    log('拼接分句音频与 full.mp3 ...')
    spans, total, stale = audio.assemble(entries, clips, game_dir / 'audio', voices['audio'], log=log)
    rows = []
    for e, p, span, clip in zip(entries, params, spans, clips):
        rows.append({'index': e['index'], 'id': e['id'], 'start': span['start'], 'end': span['end'],
                     'duration': round(span['end'] - span['start'], 3), 'voice_end': span['voice_end'],
                     'speaker': e['speaker'], 'speaker_name': e['speaker_name'], 'channel': e['channel'],
                     'day': e['day'], 'phase': e['phase'], 'phase_label': e['phase_label'], 'kind': e['kind'],
                     'is_narration': e['is_narration'], 'actor': e['actor'], 'text': e['text'],
                     'audio': span['audio'], 'start_sample': span['start_sample'], 'end_sample': span['end_sample'],
                     'voice': p, 'tts_cache': clip.name})
    timeline = {'schema_version': 1, 'game_id': data['game_id'],
                'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                'audio': 'audio/full.mp3', 'duration': round(total, 3), 'sample_rate': voices['audio']['sample_rate'],
                'voices': 'tools/tts/voices.json', 'transcript': 'transcript.json',
                'notes': '时间单位为秒；end 含句后停顿，voice_end 为语音结束；相邻条目 end == 下一条 start。',
                'entries': rows}
    write_json(game_dir / 'timeline.json', timeline)
    log(f'timeline.json：{len(rows)} 条，总时长 {total / 60:.1f} 分钟；清理旧分句文件 {stale} 个')
    return timeline


def main():
    parser = argparse.ArgumentParser(description='狼人杀整局文字整编与语音回放')
    parser.add_argument('--game', default=str(ROOT / 'games' / 'current'), help='对局目录，默认 games/current')
    parser.add_argument('--public', help='公共群 ID（写入 <对局>/tts.json）')
    parser.add_argument('--wolves', nargs='+', help='狼队群 ID，可传多个（狼人发言分散在几个群时全部列出；写入 <对局>/tts.json）')
    parser.add_argument('--offline', action='store_true', help='不联网拉群聊，使用 <对局>/.tts/ 中上次的原始结果')
    parser.add_argument('--transcript-only', action='store_true', help='只生成 transcript.json')
    parser.add_argument('--concurrency', type=int, default=3, help='edge-tts 并发数')
    parser.add_argument('--no-verify', action='store_true', help='跳过最后的核验')
    parser.add_argument('--samples', type=int, default=8, help='核验时随机抽检的句数')
    parser.add_argument('--seed', type=int, help='抽检随机种子（默认随机并记录）')
    parser.add_argument('--asr', action='store_true', help='抽检时用 faster-whisper 识别语音并与文本比对')
    parser.add_argument('--asr-model-dir', help='faster-whisper 模型目录')
    args = parser.parse_args()
    game_dir = Path(args.game)
    if not (game_dir / 'events.jsonl').exists():
        sys.exit(f'{game_dir}/events.jsonl 不存在')
    config = load_config(game_dir, args)
    fetched = step_fetch(game_dir, config, args.offline)
    data = step_transcript(game_dir, config, fetched)
    if args.transcript_only:
        return
    step_audio(game_dir, data, args.concurrency)
    if not args.no_verify:
        import verify
        ok = verify.run(game_dir, samples=args.samples, seed=args.seed, asr=args.asr,
                        asr_model_dir=args.asr_model_dir, log=log)
        sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
