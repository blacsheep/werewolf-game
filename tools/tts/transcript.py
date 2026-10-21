"""Merge the engine event log and the two group chats into transcript entries.

Pure functions only (no network, no audio), so the merge rules can be unit tested.
The engine's own Game class replays events.jsonl to obtain the phase of every
event and the public announcements (deaths, ballots, winner) it produced.
"""
import bisect
import datetime
import hashlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEATS = tuple(f'p{i:02}' for i in range(1, 13))
CN_DIGITS = '零一二三四五六七八九'


def cn_number(n):
    if n < 10:
        return CN_DIGITS[n]
    if n < 20:
        return '十' + (CN_DIGITS[n % 10] if n % 10 else '')
    return CN_DIGITS[n // 10] + '十' + (CN_DIGITS[n % 10] if n % 10 else '')


def seat_cn(seat):
    return cn_number(int(seat[1:])) + '号'


def votes_cn(value):
    whole = int(value)
    text = '两' if value == 2 else cn_number(whole)
    if value != whole:
        text += '点' + CN_DIGITS[int(round((value - whole) * 10))]
    return text + '票'


def join_seats(seats):
    return '、'.join(seat_cn(p) for p in seats)


# ---------------------------------------------------------------- text cleaning

CJK = r'　-〿一-鿿＀-￯'
SEAT_RE = re.compile(r'@?\s*(?<![A-Za-z0-9])p(0[1-9]|1[0-2])(?![0-9])')
ARABIC_SEAT_RE = re.compile(r'@?(?<![0-9.])(1[0-2]|[1-9])号')
END_PUNCT = '。！？；：，、…）」』”'


def clean_text(text):
    """pXX -> 几号, strip Markdown, flatten lines; meaning is left untouched."""
    lines = []
    for line in text.replace('\r', '').split('\n'):
        line = re.sub(r'^\s*(?:[-*+]|\d+[.)])\s+', '', line)       # list markers
        line = re.sub(r'^\s*#{1,6}\s*', '', line)                    # headings
        line = re.sub(r'^\s*>\s?', '', line)                         # quotes
        line = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', line)          # links
        line = re.sub(r'(\*\*|__|\*|`)', '', line)                   # emphasis / code
        line = SEAT_RE.sub(lambda m: seat_cn('p' + m.group(1)), line)
        line = ARABIC_SEAT_RE.sub(lambda m: cn_number(int(m.group(1))) + '号', line)
        line = line.replace('@', '')
        line = re.sub(rf'(?<=[{CJK}])\s+(?=[{CJK}])', '', line)
        line = re.sub(r'\s+', ' ', line).strip()
        if line:
            lines.append(line)
    out = ''
    for i, line in enumerate(lines):
        out += line
        if i < len(lines) - 1 and line[-1] not in END_PUNCT:
            out += '。'
    return out


def tts_text(text):
    """Pronunciation-only tweaks; the transcript keeps the cleaned text verbatim."""
    text = re.sub(r'\(([A-Za-z ]*)\)', r' \1 ', text)
    return re.sub(r'\s+', ' ', text.replace('_', ' ')).strip()


# ---------------------------------------------------------------- chat filtering

META_RE = re.compile(r'主持阻塞|阻塞报告|工作汇报|未解决事项|mapping_required|conversation_id|next_step'
                     r'|export_replay|game_over|被引擎拒绝|引擎拒绝|只有 ?note|仅剩 ?note')
VOTE_ACK_RE = re.compile(r'^\s*已投票[。.!！]?\s*$')
# A player's bare sign-up or withdrawal (older games said these in public); the host announces the list.
SIGNUP_ACK_RE = re.compile(r'^\s*我?(不?上警|不?退水|继续竞选(警长)?)([，,]\s*(继续竞选(警长)?|不退水|不上警))?[。.!！]?\s*$')
# Agents may be named per game (g001-god, g001-p03); the seat is what matters.
SENDER_RE = re.compile(r'^(?:g\d+-)?(god|p(?:0[1-9]|1[0-2]))$')


def chat_sender(message):
    """'god' or 'pXX' for host/player senders (with or without a gNNN- prefix), else None."""
    m = SENDER_RE.match(message.get('sender_id') or '')
    return m.group(1) if m else None


def chat_channel(source):
    """Channel of a chat source key: 'public', 'wolves', or 'wolves@<群ID>' (one of several wolf groups) -> 'wolves'."""
    return source.split('@', 1)[0]


def classify_chat(message, channel, speech_index, overrides):
    """Return None to keep the message, or a reason string to exclude it.

    `channel` is the chat source key, so overrides read "wolves@<群ID>:<seq>" when there are several wolf groups."""
    key = f"{channel}:{message['seq']}"
    if key in overrides.get('exclude', {}):
        return '人工排除：' + overrides['exclude'][key]
    if key in overrides.get('include', []):
        return None
    if message.get('deleted_at'):
        return '已撤回的消息'
    sender = chat_sender(message)
    body = message['body']
    if sender is None:
        return f"非主持/玩家发送者（{message['sender_id']}）"
    if sender != 'god' and VOTE_ACK_RE.match(body):
        return '投票确认“已投票”，不录入'
    if sender != 'god' and SIGNUP_ACK_RE.match(body):
        return '上警/退水表态，由主持人统一公布，不录入'
    if sender != 'god' and (sender, body.strip()) in speech_index:
        return f"与事件日志第 {speech_index[(sender, body.strip())]} 条发言逐字重复，以事件日志为准"
    if META_RE.search(body):
        return '非对局内容：主持/工具状态报告'
    return None


# ---------------------------------------------------------------- engine replay

def load_engine(path=ROOT / 'engine' / 'server.py'):
    spec = importlib.util.spec_from_file_location('ww_engine_replay', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PHASE_NAMES = {
    'night_guard': '守卫行动', 'night_wolves': '狼人行动', 'night_witch': '女巫行动',
    'night_seer': '预言家行动', 'night_start': '入夜', 'sheriff_join': '警长竞选报名',
    'sheriff_speech': '警上发言', 'withdraw': '警上退水', 'sheriff_pk_speech': '警长PK发言',
    'vote:sheriff': '警长投票', 'vote:exile': '放逐投票', 'vote_result:sheriff': '警长票型',
    'vote_result:exile': '放逐票型', 'announce': '公布死讯', 'choose_order': '警长定序',
    'day_speech': '白天发言', 'exile_pk_speech': '放逐PK发言', 'last_words': '遗言',
    'badge': '移交警徽', 'shoot': '猎人开枪', 'finished': '游戏结束',
}


def phase_key(game):
    phase = game.phase
    if phase in ('vote', 'vote_result'):
        phase += ':' + game.vote_state['kind']
    return phase


def phase_label(day, phase):
    name = PHASE_NAMES.get(phase, phase)
    if phase.startswith('night_'):
        return f'第{day}夜 · {name}'
    return f'第{day}天 · {name}'


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='microseconds')


def epoch(at):
    return datetime.datetime.fromisoformat(at).timestamp()


def narration_for_history(item):
    kind = item['kind']
    if kind == 'night_started':
        return f"进入第{cn_number(item['day'])}夜。"
    if kind == 'night_deaths':
        if not item['players']:
            return '天亮了，昨夜是平安夜。'
        return f"天亮了，昨夜死亡的是{join_seats(item['players'])}。"
    if kind == 'vote_result':
        ballots = item['ballots']
        groups = {}
        for voter in SEATS:
            if voter in ballots:
                groups.setdefault(ballots[voter], []).append(voter)
        title = '警长竞选' if item['vote_kind'] == 'sheriff' else '放逐'
        if item.get('round', 1) > 1:
            title += 'PK'
        parts = [f'{join_seats(v)}投给{seat_cn(t)}' for t, v in groups.items() if t]
        if None in groups:
            parts.append(f'{join_seats(groups[None])}弃票')
        text = f"{title}投票结果：{'；'.join(parts)}。"
        heavy = [p for p, w in item['weights'].items() if w != 1]
        if heavy:
            text += f"警长{join_seats(heavy)}的票计一点五票。"
        totals = sorted(((n, p) for p, n in item['totals'].items() if n), key=lambda x: (-x[0], x[1]))
        if totals:
            text += '，'.join(f'{seat_cn(p)}{votes_cn(n)}' for n, p in totals) + '。'
        return text
    if kind == 'sheriff_elected':
        return f"{seat_cn(item['sheriff'])}当选警长。" if item['sheriff'] else '本局没有警长。'
    if kind == 'exiled':
        return f"{seat_cn(item['player'])}被放逐出局。"
    if kind == 'no_exile':
        return '本轮无人被放逐。'
    if kind == 'badge_lost':
        return '警徽流失。'
    if kind == 'game_over':
        return '游戏结束，' + ('好人阵营获胜。' if item['winner'] == 'good' else '狼人阵营获胜。')
    return None


def replay(events, engine):
    """Replay events; yield (event, phase_before, day_before, game, new_history, extra)."""
    game = engine.Game()
    for event in events:
        before_phase, before_day = (phase_key(game), game.day) if game.started else ('setup', 0)
        extra = {}
        # What the host was asked to say just before this event, e.g. the vote result
        # before the advance that settles it.
        extra['host_says'] = ([c['say'] for c in game.next_step({})['calls']]
                              if game.started and game.phase != 'finished' else [])
        if event['action'] == 'witch':
            extra['knife'] = game.night['kill']
        seen = len(game.history) if game.started else 0
        game.apply(event['actor'], event['action'], event['args'])
        new = game.history[seen:]
        if event['action'] == 'inspect':
            extra['alignment'] = game.inspections[-1]['alignment']
        yield event, before_phase, before_day, game, new, extra


SKIPPED_HISTORY = {'speech', 'last_words', 'explosion', 'sheriff_registration', 'withdrawal',
                   'speech_direction', 'badge_transferred', 'badge_destroyed', 'hunter_shot'}


def event_narration(event, extra):
    """God-view narration for one player action, or None if the action is not narrated."""
    a, act, args = event['actor'], event['action'], event['args']
    t = args.get('target')
    if act == 'guard':
        if t is None:
            return f'守卫{seat_cn(a)}本夜空守。', f'private:{a}'
        return (f'守卫{seat_cn(a)}守护了自己。' if t == a else f'守卫{seat_cn(a)}守护了{seat_cn(t)}。'), f'private:{a}'
    if act == 'wolf_kill':
        if t is None:
            return '狼人本夜选择空刀。', 'wolves'
        return f'狼人选择袭击{seat_cn(t)}，由{seat_cn(a)}动手。', 'wolves'
    if act == 'witch':
        if args['mode'] == 'save':
            return f"女巫{seat_cn(a)}使用解药，救下了{seat_cn(extra['knife'])}。", f'private:{a}'
        if args['mode'] == 'poison':
            return f'女巫{seat_cn(a)}使用毒药，毒了{seat_cn(t)}。', f'private:{a}'
        return f'女巫{seat_cn(a)}本夜没有用药。', f'private:{a}'
    if act == 'inspect':
        result = '狼人' if extra['alignment'] == 'wolf' else '好人'
        return f'预言家{seat_cn(a)}查验了{seat_cn(t)}，结果是{result}。', f'private:{a}'
    if act == 'choose_order':
        return f"警长{seat_cn(a)}选择{'警左' if args['direction'] == 'left' else '警右'}发言。", 'public'
    if act == 'badge':
        return (f'{seat_cn(a)}将警徽移交给{seat_cn(t)}。' if t else f'{seat_cn(a)}撕毁了警徽。'), 'public'
    if act == 'shoot':
        return (f'猎人{seat_cn(a)}开枪带走了{seat_cn(t)}。' if t else f'猎人{seat_cn(a)}选择不开枪。'), 'public'
    if act == 'explode':
        return f'{seat_cn(a)}自爆出局。', 'public'
    return None


def group_narration(action, group):
    field = 'join' if action == 'sheriff_join' else 'withdraw'
    yes = sorted(e['actor'] for e in group if e['args'][field])
    no = sorted(e['actor'] for e in group if not e['args'][field])
    if action == 'sheriff_join':
        if not yes:
            return '警长竞选报名结束：无人上警。'
        return f'警长竞选报名结束：{join_seats(yes)}上警' + (f'；{join_seats(no)}不上警。' if no else '。')
    return '退水结果：' + (f'{join_seats(yes)}退水' if yes else '无人退水') + (
        f'；{join_seats(no)}留在警上。' if no else '。')


# Words that belong to the machinery, not to a game viewers listen to.
TECH_TALK = re.compile(r'调用|工具|提交|引擎|MCP|mcp|next_step|my_info|wolf_kill|advance|\bp(0[1-9]|1[0-2])\b|\[\[')


def tech_talk(entries):
    """Entries whose text would read tool or engine talk aloud."""
    return [e for e in entries if TECH_TALK.search(e['text'])]


def build(events, chats, seats=None, overrides=None, engine=None):
    """events: parsed events.jsonl; chats: {'public': [msgs], 'wolves': [msgs]}, or one key
    'wolves@<群ID>' per group when the wolves talked in several groups (each keeps its own seq).

    Returns (entries, excluded, counts, coverage). Every event seq maps to the
    transcript ids that cover it, or to the reason it is not voiced.
    """
    engine = engine or load_engine()
    overrides = overrides or {}
    names = {p: (seats or {}).get(p, {}).get('display_name', f'{int(p[1:])}号') for p in SEATS}
    names.update(god='上帝', narrator='旁白')
    items = []        # entry dicts with a private '_key' used for ordering
    coverage = {}     # seq -> list of entry dicts, or a reason string
    snapshots = []    # (time, day, phase) after each event, for placing chat messages
    pending = []      # consecutive sheriff_join / withdraw events, announced as one line
    notice = None     # the host's announcement once the sign-ups or withdrawals are complete
    host_lines = {clean_text(m['body']) for m in chats.get('public', []) if chat_sender(m) == 'god'}
    said_at = {}      # host line -> [(time the step ended, day, phase)] for each step that asked for it
    announced = {}    # host line -> [(event time, seq)] of the events whose outcome it announces
    public_talk = [(m['created_at'] / 1000, chat_sender(m)) for m in chats.get('public', [])]
    speech_index = {}

    def add(key, entry, seqs):
        entry['_key'] = key
        items.append(entry)
        for s in seqs:
            if not isinstance(coverage.get(s), list):
                coverage[s] = []
            coverage[s].append(entry)

    def narrate(event, text, channel, phase, day, actor=None, sub=0, seqs=None, source=None):
        add((epoch(event['at']), 0, event['seq'], sub), {
            'speaker': 'narrator', 'channel': channel, 'phase': phase, 'day': day,
            'kind': 'narration', 'is_narration': True, 'actor': actor, 'text': text, 'at': event['at'],
            'source': source or {'type': 'event', 'seq': event['seq'], 'action': event['action']},
        }, seqs or [event['seq']])

    def flush():
        nonlocal notice
        if pending:
            group = [e for e, _, _ in pending]
            _, phase, day = pending[0]
            seqs = [e['seq'] for e in group]
            if notice and notice in host_lines:
                # The host announced the list in the public group; that line is voiced instead.
                for s in seqs:
                    coverage[s] = '由主持人在公共群公布'
                announced.setdefault(notice, []).append((epoch(group[-1]['at']), group[-1]['seq']))
            else:
                narrate(group[-1], notice or group_narration(group[0]['action'], group), 'public', phase, day,
                        seqs=seqs, source={'type': 'event', 'seq': seqs, 'action': group[0]['action']})
            pending.clear()
            notice = None

    for event, phase, day, game, new, extra in replay(events, engine):
        a, act, args, seq = event['actor'], event['action'], event['args'], event['seq']
        snapshots.append((epoch(event['at']), game.day, phase_key(game)))
        for say in extra['host_says']:
            said_at.setdefault(clean_text(say), []).append((epoch(event['at']), day, phase))
        voiced = any(clean_text(say) in host_lines for say in extra['host_says'])
        if pending and act not in (pending[0][0]['action'], 'note'):
            flush()
        if act == 'start_game':
            narrate(event, '对局开始，进入第一夜。', 'public', 'night_start', 1)
            continue
        if act in ('sheriff_join', 'withdraw'):
            pending.append((event, phase, day))
            notice = getattr(game, 'notice', None) or notice
        elif act in ('speak', 'last_words'):
            speech_index[(a, args['text'].strip())] = seq
            add((epoch(event['at']), 0, seq, 0), {
                'speaker': a, 'channel': 'public', 'phase': phase, 'day': day,
                'kind': 'speech' if act == 'speak' else 'last_words', 'is_narration': False,
                'actor': a, 'text': clean_text(args['text']), 'at': event['at'],
                'source': {'type': 'event', 'seq': seq, 'action': act}}, [seq])
        elif act == 'vote':
            coverage[seq] = []        # secret ballot, voiced when the engine announces the result
        elif act == 'note':
            coverage[seq] = '私密笔记，不属于发言或技能操作，不录入'
        elif act == 'choose_order' and any(sender == a and 0 <= t - epoch(event['at']) <= 300
                                           for t, sender in public_talk):
            coverage[seq] = '警长在公共群自己说了发言方向'
        else:
            narration = event_narration(event, extra)
            if narration:
                narrate(event, narration[0], narration[1], phase, day, actor=a)
        if act == 'advance' and voiced:
            # The host read this announcement out in the public group; do not repeat it.
            coverage[seq] = '由主持人在公共群公布'
            for say in extra['host_says']:
                if clean_text(say) in host_lines:
                    announced.setdefault(clean_text(say), []).append((epoch(event['at']), seq))
            for e in events:
                if e['action'] == 'vote' and e['seq'] < seq and coverage.get(e['seq']) == []:
                    coverage[e['seq']] = '秘密投票，票型由主持人在公共群公布'
            continue
        # public announcements the engine produced for this event (advance, explosion, exile ...)
        for sub, item in enumerate(new, 1):
            text = None if item['kind'] in SKIPPED_HISTORY else narration_for_history(item)
            if item['kind'] == 'sheriff_elected' and act in ('sheriff_join', 'withdraw') and notice:
                text = None       # the sign-up or withdrawal announcement already names the sheriff
            if not text:
                continue
            h_phase, h_day = ('night_start', item['day']) if item['kind'] == 'night_started' else (phase, day)
            seqs, source = [seq], {'type': 'engine_announcement', 'seq': seq, 'kind': item['kind']}
            if item['kind'] == 'vote_result':
                ballots = [e['seq'] for e in events if e['action'] == 'vote' and e['seq'] < seq
                           and coverage.get(e['seq']) == []]
                seqs += ballots
                source['ballot_seqs'] = ballots
            narrate(event, text, 'public', h_phase, h_day, sub=sub, seqs=seqs, source=source)
        if act == 'advance' and seq not in coverage:
            coverage[seq] = '推进未产生公告'
    flush()

    times = [s[0] for s in snapshots]
    excluded, counts = [], {}
    for key, messages in chats.items():
        channel = chat_channel(key)
        counts[key] = {'messages': len(messages), 'included': 0, 'excluded': 0}
        called = None    # when the host last spoke in this group
        for m in sorted(messages, key=lambda m: m['seq']):
            at = iso(m['created_at'])
            reason = classify_chat(m, key, speech_index, overrides)
            if reason:
                counts[key]['excluded'] += 1
                excluded.append({'channel': channel, 'chat': key, 'conversation_id': m.get('conversation_id'),
                                 'seq': m['seq'], 'message_id': m['id'],
                                 'sender': m['sender_id'], 'at': at, 'reason': reason,
                                 'preview': m['body'][:40]})
                continue
            # A player's line belongs to the phase of the host call it answers: players act
            # first and speak after, by which time the engine has moved to the next phase.
            if chat_sender(m) == 'god':
                called = m['created_at']
            placed = m['created_at'] if called is None else called
            i = bisect.bisect_right(times, placed / 1000) - 1
            day, phase = (snapshots[i][1], snapshots[i][2]) if i >= 0 else (0, 'setup')
            if chat_sender(m) == 'god' and clean_text(m['body']) in said_at:
                # A host line belongs to the step that asked for it, even when sent shortly
                # after that step ended (results are read out after they are settled).
                steps = said_at[clean_text(m['body'])]
                _, day, phase = next((s for s in steps if s[0] >= m['created_at'] / 1000 - 120), steps[-1])
            sender = chat_sender(m)
            if sender is None:        # only reachable through a manual include
                raise ValueError(f"{key}:{m['seq']} 的发送者 {m['sender_id']} 不是上帝或座位，不能录入")
            counts[key]['included'] += 1
            source = {'type': 'chat', 'conversation': channel, 'chat': key,
                      'conversation_id': m.get('conversation_id'), 'seq': m['seq'], 'message_id': m['id'],
                      'sender_id': m['sender_id']}
            if sender == 'god' and clean_text(m['body']) in announced:
                # The event whose outcome this line reads out, for the replay's seat states.
                steps = announced[clean_text(m['body'])]
                source['announces'] = next((q for t, q in steps if t >= m['created_at'] / 1000 - 120), steps[-1][1])
            add((m['created_at'] / 1000, 1, m['seq'], key), {
                'speaker': sender, 'channel': channel, 'phase': phase, 'day': day,
                'kind': 'host' if sender == 'god' else ('wolf_chat' if channel == 'wolves' else 'chat'),
                'is_narration': False, 'actor': None if sender == 'god' else sender,
                'text': clean_text(m['body']), 'at': at, 'source': source}, [])

    items.sort(key=lambda e: e['_key'])
    entries = []
    for index, item in enumerate(items, 1):
        item['id'] = f'L{index:04d}'
        entries.append({'index': index, 'id': item['id'], 'speaker': item['speaker'],
                        'speaker_name': names[item['speaker']], 'channel': item['channel'],
                        'day': item['day'], 'phase': item['phase'],
                        'phase_label': phase_label(item['day'], item['phase']),
                        'kind': item['kind'], 'is_narration': item['is_narration'], 'actor': item['actor'],
                        'text': item['text'], 'tts_text': tts_text(item['text']),
                        'at': item['at'], 'source': item['source']})
    final = {}
    for event in events:
        value = coverage.get(event['seq'])
        if isinstance(value, list):
            value = [e['id'] for e in value] or '未覆盖'
        final[str(event['seq'])] = value or '未覆盖'
    return entries, excluded, counts, final


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
