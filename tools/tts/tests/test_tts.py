"""Offline tests for tools/tts (no network, no edge-tts). Run:
    python3 -m unittest discover -s tools/tts/tests -v
"""
import datetime
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))
import audio  # noqa: E402
import chats  # noqa: E402
import transcript as T  # noqa: E402

BASE = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)


def at(seconds):
    return (BASE + datetime.timedelta(seconds=seconds)).isoformat()


def ms(seconds):
    return int((BASE + datetime.timedelta(seconds=seconds)).timestamp() * 1000)


class Recorder:
    """Drive the real engine Game and record events like events.jsonl."""

    def __init__(self, engine, seed=7):
        self.engine, self.game, self.events, self.t = engine, engine.Game(), [], 0
        self.do('god', 'start_game', {'seed': seed})
        self.roles = self.game.roles

    def do(self, actor, action, args):
        self.t += 10
        self.game.apply(actor, action, args)
        self.events.append({'version': 1, 'seq': len(self.events) + 1, 'at': at(self.t), 'actor': actor,
                            'action': action, 'args': args, 'prev': '', 'hash': f'h{len(self.events) + 1}'})
        return self.t

    def seat(self, role, n=0):
        return [p for p in T.SEATS if self.roles[p] == role][n]


def msg(seq, sender, body, seconds, conversation='c'):
    return {'id': f'm{conversation}{seq}', 'seq': seq, 'sender_id': sender, 'body': body,
            'created_at': ms(seconds), 'deleted_at': None, 'conversation_id': conversation}


class TextTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual([T.cn_number(n) for n in (1, 9, 10, 11, 12)], ['一', '九', '十', '十一', '十二'])
        self.assertEqual(T.votes_cn(7.5), '七点五票')
        self.assertEqual(T.votes_cn(2), '两票')
        self.assertEqual(T.votes_cn(1), '一票')

    def test_clean(self):
        self.assertEqual(T.clean_text('请 @p01 发言。'), '请一号发言。')
        self.assertEqual(T.clean_text('统一刀p06，由 p12 提交'), '统一刀六号，由十二号提交')
        self.assertEqual(T.clean_text('8号请按点名顺序发言，警长1.5票'), '八号请按点名顺序发言，警长1.5票')
        self.assertEqual(T.clean_text('- **终局：**引擎 `finished`\n- 胜方：好人'), '终局：引擎 finished。胜方：好人')
        self.assertEqual(T.clean_text('对局结束：好人胜利。\n\n全员身份：p01 村民；p02 狼人。'),
                         '对局结束：好人胜利。全员身份：一号村民；二号狼人。')
        self.assertEqual(T.clean_text('p13 与 step1 不是座位'), 'p13 与 step1 不是座位')

    def test_tts_text(self):
        self.assertEqual(T.tts_text('提交 wolf_kill(target)。'), '提交 wolf kill target 。')

    def test_classify(self):
        idx = {('p03', '我的发言'): 12}
        c = lambda body, sender='p03', ov=None: T.classify_chat(msg(1, sender, body, 0), 'public', idx, ov or {})  # noqa: E731
        self.assertIn('已投票', c('已投票'))
        self.assertIn('重复', c('我的发言'))
        self.assertIn('非对局', c('主持阻塞：next_step 返回 conversation_id:null', 'god'))
        self.assertIn('非对局', c('狼刀提交被引擎拒绝', 'p02'))
        self.assertIsNone(c('请三号发言。', 'god'))
        self.assertIsNone(c('收到，统一刀一号由四号提交。', 'p02'))
        self.assertIn('人工排除', c('请三号发言。', 'god', {'exclude': {'public:1': '测试'}}))
        self.assertIsNone(c('已投票', 'p03', {'include': ['public:1']}))
        self.assertIn('非主持', c('hello', 'lead'))
        # per-game agent names map to the same seats
        self.assertIsNone(c('请三号发言。', 'g001-god'))
        self.assertIn('重复', c('我的发言', 'g001-p03'))
        self.assertIn('已投票', c('已投票', 'g001-p03'))
        self.assertIn('非主持', c('hello', 'g001-voice'))
        self.assertIn('非主持', c('hello', 'g001-p13'))
        self.assertEqual([T.chat_sender({'sender_id': s}) for s in ('g001-god', 'g012-p12', 'p01', 'g001-lead')],
                         ['god', 'p12', 'p01', None])


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = T.load_engine()
        r = Recorder(engine)
        guard, witch, seer = r.seat('guard'), r.seat('witch'), r.seat('seer')
        wolf, wolf2 = r.seat('wolf'), r.seat('wolf', 1)
        victim = r.seat('villager')
        r.do(guard, 'guard', {'target': guard})
        cls.t_wolf_chat = r.t + 2
        r.do(wolf, 'wolf_kill', {'target': victim})
        r.do(witch, 'witch', {'mode': 'save', 'target': None})
        cls.t_after_kill = r.t + 1
        r.do(seer, 'inspect', {'target': wolf})
        for i, p in enumerate(T.SEATS):
            if i == 3:
                r.do(seer, 'note', {'text': '私密笔记'})
            r.do(p, 'sheriff_join', {'join': False})
        r.do('god', 'advance', {})
        cls.speeches = []
        cls.t_prompt = r.t + 5
        while r.game.phase == 'day_speech':
            p = r.game.speakers[0]
            text = f'我是{p}，我怀疑 p03。'
            r.do(p, 'speak', {'text': text})
            cls.speeches.append((p, text, r.t))
        voters = list(r.game.vote_state['voters'])
        for p in voters:
            r.do(p, 'vote', {'target': wolf})
        r.do('god', 'advance', {})
        r.do(wolf, 'last_words', {'text': '我是好人。'})
        cls.r, cls.wolf, cls.wolf2, cls.victim, cls.voters = r, wolf, wolf2, victim, voters
        sp_p, sp_text, sp_t = cls.speeches[0]
        public = [msg(1, 'god', '请 @' + sp_p + ' 发言。', cls.t_prompt, 'pub'),
                  msg(2, sp_p, sp_text, sp_t + 3, 'pub'),
                  msg(3, voters[0], '已投票', cls.speeches[-1][2] + 15, 'pub'),
                  msg(4, 'god', '主持阻塞：`next_step` 返回 mapping_required', r.t + 1, 'pub')]
        wolves = [msg(1, 'god', '狼人行动：请读取 my_info。', cls.t_wolf_chat - 1, 'wolf'),
                  msg(2, wolf2, f'我提议刀 {victim}。', cls.t_wolf_chat, 'wolf'),
                  msg(3, wolf, '已经动手了。', cls.t_after_kill, 'wolf')]
        cls.entries, cls.excluded, cls.counts, cls.cov = T.build(r.events, {'public': public, 'wolves': wolves},
                                                                 engine=engine)

    def kinds(self, kind):
        return [e for e in self.entries if e['kind'] == kind]

    def test_every_event_covered(self):
        self.assertNotIn('未覆盖', self.cov.values())
        self.assertEqual(len(self.cov), len(self.r.events))

    def test_night_narration_channels(self):
        n = {e['source'].get('action'): e for e in self.kinds('narration')}
        self.assertEqual(n['guard']['channel'], f"private:{self.r.seat('guard')}")
        self.assertIn('守护了自己', n['guard']['text'])
        self.assertEqual(n['wolf_kill']['channel'], 'wolves')
        self.assertIn(T.seat_cn(self.victim), n['witch']['text'])
        self.assertIn('解药', n['witch']['text'])
        self.assertIn('结果是狼人', n['inspect']['text'])
        self.assertTrue(all(e['speaker'] == 'narrator' and e['is_narration'] for e in self.kinds('narration')))

    def test_sheriff_join_single_line_despite_note(self):
        lines = [e for e in self.entries if e['source'].get('action') == 'sheriff_join']
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0]['source']['seq']), 12)

    def test_sign_ups_are_voiced_once_by_the_host_when_announced(self):
        # Without a host line the narrator reads the engine's short announcement.
        line = next(e for e in self.entries if e['source'].get('action') == 'sheriff_join')
        self.assertEqual(line['text'], '没有人上警，本局没有警长。')
        # With it, only the host's line is voiced and bare sign-ups in chat are dropped.
        t = self.r.events[[e['action'] for e in self.r.events].index('sheriff_join')]['at']
        host = msg(10, 'god', '没有人上警，本局没有警长。', T.epoch(t) + 1, 'pub')
        ack = msg(11, T.SEATS[0], '我不上警。', T.epoch(t) + 2, 'pub')
        entries, excluded, _, cov = T.build(self.r.events, {'public': [host, ack], 'wolves': []}, engine=T.load_engine())
        self.assertFalse(any(e['source'].get('action') == 'sheriff_join' for e in entries))
        self.assertEqual([e['text'] for e in entries if e['text'] == host['body']], [host['body']])
        self.assertTrue(any('上警/退水表态' in x['reason'] for x in excluded))
        self.assertTrue(all(cov[str(e['seq'])] == '由主持人在公共群公布'
                            for e in self.r.events if e['action'] == 'sheriff_join'))

    def test_a_result_the_host_read_out_is_not_narrated_again(self):
        engine = T.load_engine()
        game, say, at = engine.Game(), None, None
        for e in self.r.events:
            if e['action'] == 'advance' and game.started and game.phase == 'vote_result':
                say, at = game.next_step({})['calls'][-1]['say'], T.epoch(e['at'])
                break
            game.apply(e['actor'], e['action'], e['args'])
        host = msg(20, 'god', say, at + 30, 'pub')
        entries, _, _, cov = T.build(self.r.events, {'public': [host], 'wolves': []}, engine=engine)
        self.assertFalse(any(e['source'].get('kind') == 'vote_result' for e in entries))
        line = next(e for e in entries if e['text'] == T.clean_text(say))
        self.assertEqual(line['phase'], 'vote_result:exile')
        self.assertIn('由主持人在公共群公布', cov.values())

    def test_votes_announced_once(self):
        res = [e for e in self.entries if e['source'].get('kind') == 'vote_result']
        self.assertEqual(len(res), 1)
        self.assertEqual(len(res[0]['source']['ballot_seqs']), len(self.voters))
        self.assertIn(f'投给{T.seat_cn(self.wolf)}', res[0]['text'])
        self.assertTrue(any(e['text'] == f'{T.seat_cn(self.wolf)}被放逐出局。' for e in self.entries))

    def test_speeches_from_events_and_chat_dedup(self):
        speeches = self.kinds('speech')
        self.assertEqual(len(speeches), len(self.speeches))
        self.assertTrue(all('p0' not in e['text'] and 'p1' not in e['text'] for e in speeches))
        self.assertEqual(speeches[0]['phase'], 'day_speech')
        reasons = sorted(x['reason'][:4] for x in self.excluded)
        self.assertEqual(len(self.excluded), 3, self.excluded)
        self.assertTrue(any('重复' in x['reason'] for x in self.excluded))
        self.assertTrue(any('已投票' in x['reason'] for x in self.excluded))
        self.assertTrue(any('非对局' in x['reason'] for x in self.excluded), reasons)
        self.assertEqual(self.kinds('last_words')[0]['phase'], 'last_words')

    def test_chat_channels_and_order(self):
        wolf_chat = self.kinds('wolf_chat')
        self.assertEqual(len(wolf_chat), 2)
        # Wolves talk on after the kill, while the engine is already in the witch's turn.
        for line in wolf_chat:
            self.assertEqual(line['channel'], 'wolves')
            self.assertEqual(line['phase'], 'night_wolves')
        self.assertEqual(wolf_chat[0]['text'], f'我提议刀{T.seat_cn(self.victim)}。')
        hosts = self.kinds('host')
        self.assertEqual({h['channel'] for h in hosts}, {'public', 'wolves'})
        keys = [T.epoch(e['at']) for e in self.entries]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([e['index'] for e in self.entries], list(range(1, len(self.entries) + 1)))
        self.assertEqual(self.entries[0]['text'], '对局开始，进入第一夜。')
        self.assertTrue(any(e['text'] == '进入第二夜。' for e in self.entries))


class MultiWolfGroupTests(unittest.TestCase):
    """Wolves may talk in several groups: every group is read and each line keeps its own group."""

    def setUp(self):
        engine = T.load_engine()
        r = Recorder(engine)
        wolf, wolf2 = r.seat('wolf'), r.seat('wolf', 1)
        r.do(r.seat('guard'), 'guard', {'target': None})
        self.t0 = r.t
        r.do(wolf, 'wolf_kill', {'target': r.seat('villager')})
        self.r, self.engine, self.wolf, self.wolf2 = r, engine, wolf, wolf2
        # The same seq numbers in two groups; the second group's lines interleave in time.
        self.chats = {
            'public': [],
            'wolves@A': [msg(1, 'god', '狼人请睁眼。', self.t0 + 1, 'A'), msg(2, wolf, '刀一号。', self.t0 + 3, 'A')],
            'wolves@B': [msg(1, 'god', '狼人请睁眼。', self.t0 + 2, 'B'), msg(2, wolf2, '我同意。', self.t0 + 4, 'B'),
                         msg(3, wolf2, '我来动手。', self.t0 + 5, 'B')],
        }

    def test_both_groups_voiced_with_their_source(self):
        entries, excluded, counts, _ = T.build(self.r.events, self.chats, engine=self.engine)
        chat = [e for e in entries if e['source']['type'] == 'chat']
        self.assertEqual([(e['source']['conversation_id'], e['source']['seq']) for e in chat],
                         [('A', 1), ('B', 1), ('A', 2), ('B', 2), ('B', 3)])
        self.assertTrue(all(e['channel'] == 'wolves' and e['source']['chat'] == 'wolves@' + e['source']['conversation_id']
                            for e in chat))
        self.assertEqual([e['speaker'] for e in chat], ['god', 'god', self.wolf, self.wolf2, self.wolf2])
        self.assertEqual(counts['wolves@A']['included'], 2)
        self.assertEqual(counts['wolves@B']['included'], 3)
        self.assertEqual(excluded, [])

    def test_overrides_are_per_group(self):
        overrides = {'exclude': {'wolves@B:2': '测试'}}
        entries, excluded, _, _ = T.build(self.r.events, self.chats, overrides=overrides, engine=self.engine)
        kept = {(e['source']['conversation_id'], e['source']['seq']) for e in entries if e['source']['type'] == 'chat'}
        self.assertIn(('A', 2), kept)
        self.assertNotIn(('B', 2), kept)
        self.assertEqual([(x['conversation_id'], x['seq'], x['chat']) for x in excluded], [('B', 2, 'wolves@B')])

    def test_pipeline_config_and_sources(self):
        import pipeline
        with tempfile.TemporaryDirectory() as d:
            game = Path(d)
            args = type('A', (), {'public': 'P', 'wolves': ['W1', 'W2', 'W3']})()
            config = pipeline.load_config(game, args)
            self.assertEqual(json.loads((game / 'tts.json').read_text())['conversations']['wolves'], ['W1', 'W2', 'W3'])
            self.assertEqual(pipeline.chat_sources(config['conversations']),
                             [('public', 'public', 'P'), ('wolves@W1', 'wolves', 'W1'),
                              ('wolves@W2', 'wolves', 'W2'), ('wolves@W3', 'wolves', 'W3')])
            # One group stays a plain string and the old 'wolves' key, as in earlier games.
            single = pipeline.load_config(Path(d), type('A', (), {'public': None, 'wolves': ['W1']})())
            self.assertEqual(single['conversations']['wolves'], 'W1')
            self.assertEqual(pipeline.chat_sources(single['conversations'])[1], ('wolves', 'wolves', 'W1'))
            with self.assertRaises(SystemExit):
                pipeline.chat_sources({'public': 'P', 'wolves': ['W1', 'W1']})

    def test_fetch_stops_when_any_group_is_unreadable(self):
        import pipeline
        read = []

        def fake_fetch(conversation):
            read.append(conversation)
            if conversation == 'W2':
                raise chats.FetchError('permission denied')
            return [msg(1, 'god', 'x', 1, conversation)], {'pages': 1, 'count': 1, 'seq_gaps': [],
                                                            'first_seq': 1, 'last_seq': 1}
        config = {'conversations': {'public': 'P', 'wolves': ['W1', 'W2', 'W3']}}
        original = pipeline.chats.fetch_all
        pipeline.chats.fetch_all = fake_fetch
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(SystemExit) as cm:
                    pipeline.step_fetch(Path(d), config, offline=False)
                self.assertIn('W2', str(cm.exception))
                config['conversations']['wolves'] = ['W1', 'W3']
                got = pipeline.step_fetch(Path(d), config, offline=False)
                self.assertEqual(list(got), ['public', 'wolves@W1', 'wolves@W3'])
                self.assertTrue((Path(d) / '.tts' / 'chat-wolves-W3.json').exists())
                offline = pipeline.step_fetch(Path(d), config, offline=True)
                self.assertEqual({k: v[0] for k, v in offline.items()}, {k: v[0] for k, v in got.items()})
        finally:
            pipeline.chats.fetch_all = original
        self.assertEqual(read, ['P', 'W1', 'W2', 'P', 'W1', 'W3'])

    def test_verify_requires_every_configured_group(self):
        import verify
        entries, _, counts, _ = T.build(self.r.events, self.chats, engine=self.engine)
        tr = {'entries': entries, 'sources': {'chats': {k: {'conversation_id': k.split('@')[-1] if '@' in k else 'P', **v}
                                                         for k, v in counts.items()}}}
        rep = verify.Report(lambda *_: None)
        verify.check_chat_sources(tr, {'conversations': {'public': 'P', 'wolves': ['A', 'B']}}, rep)
        self.assertTrue(all(c['ok'] for c in rep.checks), rep.checks)
        rep = verify.Report(lambda *_: None)
        verify.check_chat_sources(tr, {'conversations': {'public': 'P', 'wolves': ['A', 'B', 'C']}}, rep)
        self.assertFalse(rep.checks[0]['ok'])
        entries[[e['source']['type'] for e in entries].index('chat')]['source']['conversation_id'] = 'B'
        rep = verify.Report(lambda *_: None)
        verify.check_chat_sources(tr, {'conversations': {'public': 'P', 'wolves': ['A', 'B']}}, rep)
        self.assertFalse(rep.checks[1]['ok'])


class FetchTests(unittest.TestCase):
    def test_pagination_with_fake_agt(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'agt'
            pages = {None: {'messages': [msg(3, 'god', 'c', 3, 'X'), msg(4, 'god', 'd', 4, 'X')], 'next_cursor': 'k1'},
                     'k1': {'messages': [msg(1, 'god', 'a', 1, 'X'), msg(2, 'god', 'b', 2, 'X')], 'next_cursor': None}}
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nargs=json.loads(sys.argv[sys.argv.index("--args")+1])\n'
                            f'pages=json.loads({json.dumps(json.dumps({str(k): v for k, v in pages.items()}))})\n'
                            'print(json.dumps(pages[str(args.get("before"))]))\n')
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            messages, info = chats.fetch_all('X', agt=str(fake), home=d)
            self.assertEqual([m['seq'] for m in messages], [1, 2, 3, 4])
            self.assertEqual(info['pages'], 2)
            self.assertEqual(info['seq_gaps'], [])

    def test_cli_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'agt'
            fake.write_text('#!/bin/sh\necho "permission denied" >&2\nexit 3\n')
            fake.chmod(0o755)
            with self.assertRaises(chats.FetchError) as cm:
                chats.fetch_all('X', agt=str(fake), home=d)
            self.assertIn('permission denied', str(cm.exception))


class AssembleTests(unittest.TestCase):
    def test_contiguous_and_safe_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            clips = []
            for i, secs in enumerate((0.5, 1.2, 0.8)):
                p = d / f'clip{i}.mp3'
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency={300 + 100 * i}:duration={secs}',
                                '-ac', '1', '-ar', '24000', '-c:a', 'libmp3lame', '-b:a', '48k', str(p)], check=True)
                clips.append(p)
            out = d / 'audio'
            out.mkdir()
            for keep in ('notes.mp3', 'keep.txt', 'L0001-god.mp3.bak'):
                (out / keep).write_text('x')
            (out / 'L0099-p05.mp3').write_text('stale')
            entries = [{'id': f'L{i + 1:04d}', 'speaker': s, 'phase_label': pl}
                       for i, (s, pl) in enumerate([('god', 'a'), ('p01', 'a'), ('narrator', 'b')])]
            settings = {'sample_rate': 24000, 'bitrate': '48k', 'pause_after_line_ms': 450, 'pause_on_phase_change_ms': 650}
            spans, total, stale = audio.assemble(entries, clips, out, settings, log=lambda *_: None)
            self.assertEqual(spans[0]['start'], 0)
            for a, b in zip(spans, spans[1:]):
                self.assertEqual(a['end'], b['start'])
                self.assertEqual(a['end_sample'], b['start_sample'])
            self.assertAlmostEqual(spans[-1]['end'], total, places=3)
            # pause 0.45 after line 1, 0.45+0.65 after line 2 (phase change), 1.1 after the last line
            self.assertAlmostEqual(spans[1]['end'] - spans[1]['voice_end'], 1.1, places=2)
            self.assertEqual(stale, 1)
            self.assertFalse((out / 'L0099-p05.mp3').exists())
            for keep in ('notes.mp3', 'keep.txt', 'L0001-god.mp3.bak'):
                self.assertTrue((out / keep).exists(), keep)
            for s in spans:
                p = audio.probe(d / s['audio'])
                self.assertEqual(p['codec'], 'mp3')
                self.assertLess(abs(p['duration'] - (s['end'] - s['start'])), 0.08)
            self.assertLess(abs(audio.probe(out / 'full.mp3')['duration'] - total), 0.1)

    def test_voice_params_validation(self):
        voices = json.loads((TOOL_DIR / 'voices.json').read_text())
        self.assertEqual(set(voices['speakers']), {'god', 'narrator', *T.SEATS})
        combos = {tuple(audio.voice_params(voices, s).values()) for s in voices['speakers']}
        self.assertEqual(len(combos), 14, '每个说话人的音色组合必须互不相同')
        with self.assertRaises(audio.AudioError):
            audio.voice_params({'speakers': {'x': {'voice': 'v', 'rate': '10'}}}, 'x')


class SignalTests(unittest.TestCase):
    def test_spectrogram_similarity_separates_voices(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy 未安装（run.sh 环境中有）')
        import verify
        rate = 24000
        t = np.arange(rate * 2) / rate
        env = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)
        voice = lambda f0: sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8)).astype(np.float32) * env * 0.2  # noqa: E731
        a, a2, b = voice(120), voice(120) + np.float32(1e-5) * np.random.default_rng(0).standard_normal(len(t)).astype(np.float32), voice(260)
        same = verify.spectrogram_similarity(a, a2, rate)
        other = verify.spectrogram_similarity(a, b, rate)
        self.assertGreater(same, 0.95)
        self.assertGreater(same - other, 0.05)
        corr, lag = verify.best_lag(verify.envelope(a, rate), verify.envelope(a2, rate))
        self.assertGreater(corr, 0.9)
        self.assertEqual(lag, 0)


if __name__ == '__main__':
    unittest.main()
