"""Data tests; run without Chrome or modifying game inputs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
import tempfile
import subprocess
from unittest.mock import patch
from build import ROOT, check_entries, frame_cues, hashes, load_game, public_ranges, read, verify_frame_images


# Regression samples keyed by the hash of each game's opening event, so a later
# game that reuses the same id is not checked against another game's story.
SAMPLES = {
    # Game 1 (commit 84b77ee): six signed up, one and eleven withdrew; good wins on day 5.
    '996a7fe253b1299425f56896aeba00287d7e8a97c18815b6a3ba7460c5446956': {
        'deaths': [('L0046', 'L0047', ['p02']), ('L0057', 'L0058', ['p06']),
                   ('L0081', 'L0082', ['p04']), ('L0092', 'L0093', ['p07']),
                   ('L0115', 'L0116', ['p05']), ('L0146', 'L0147', ['p01']),
                   ('L0156', 'L0157', ['p03']), ('L0174', 'L0175', ['p09'])],
        'sheriff': [('L0033', 'L0034', None, 'p07'), ('L0094', 'L0095', 'p07', 'p03'),
                    ('L0158', 'L0159', 'p03', 'p10')],
        'winner': ('L0174', 'L0175', 'good'),
        'survivors': ['p08', 'p10', 'p11', 'p12'],
        'candidates': [('L0017', []), ('L0018', ['p01', 'p02', 'p05', 'p06', 'p07', 'p11']),
                       ('L0032', ['p02', 'p05', 'p06', 'p07']), ('L0034', [])],
        'roles': ['村民', '狼人', '女巫', '狼人', '村民', '狼人', '预言家', '村民', '狼人', '猎人', '村民', '守卫'],
    },
    # Game 3: four signed up, two withdrew; one and six die the same night; wolves win.
    'fdbed6d8af8f282332ee99bc4c75cb4a330fc69e6ea2e4b99a90133581a079d1': {
        'deaths': [('L0056', 'L0057', ['p05']), ('L0068', 'L0069', ['p09']),
                   ('L0092', 'L0093', ['p12']), ('L0129', 'L0130', ['p08']),
                   ('L0145', 'L0146', ['p01', 'p06']), ('L0165', 'L0166', ['p11'])],
        'sheriff': [('L0027', 'L0028', None, 'p01'), ('L0148', 'L0149', 'p01', 'p02')],
        'winner': ('L0165', 'L0166', 'wolves'),
        'survivors': ['p02', 'p03', 'p04', 'p07', 'p10'],
        'candidates': [('L0015', []), ('L0016', ['p01', 'p02', 'p05', 'p08']),
                       ('L0026', ['p01', 'p05', 'p08']), ('L0028', [])],
        'roles': ['狼人', '狼人', '狼人', '女巫', '预言家', '守卫', '狼人', '村民', '村民', '猎人', '村民', '村民'],
    },
}


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # WW_REPLAY_GAME checks another game directory, e.g. an older game from git.
        cls.game = Path(os.environ.get('WW_REPLAY_GAME') or ROOT / 'games/current')
        if not (cls.game / 'audio/full.mp3').exists():
            raise unittest.SkipTest(f'{cls.game} 不是带整局音频的完整对局')
        cls.before = hashes(cls.game)
        cls.data = load_game(cls.game)
        cls.by_id = {e['id']: e for e in cls.data['entries']}

    def test_continuous_timeline(self):
        entries = self.data['entries']
        check_entries(entries)
        for change in ('gap', 'overlap', 'duplicate', 'nan'):
            broken = copy.deepcopy(entries[:2])
            if change == 'gap': broken[1]['start'] += 1
            if change == 'overlap': broken[1]['start'] -= 1
            if change == 'duplicate': broken[1]['id'] = broken[0]['id']
            if change == 'nan': broken[1]['end'] = float('nan')
            with self.assertRaises(ValueError): check_entries(broken)

    def test_public_mapping(self):
        last = 0
        for e in self.data['entries']:
            if e['channel'] == 'public':
                self.assertAlmostEqual(e['public_start'], last, places=5)
                self.assertAlmostEqual(e['public_end']-e['public_start'], e['end']-e['start'], places=5)
                last = e['public_end']
            else:
                self.assertIsNone(e['public_start'])
                self.assertIsNone(e['public_end'])
        self.assertAlmostEqual(last, self.data['public_duration'])
        self.assertLess(last, self.data['duration'])

    def test_public_audio_ranges_exclude_every_private_sample(self):
        ranges = public_ranges(self.data['entries'])
        self.assertAlmostEqual(sum(end-start for start, end in ranges), self.data['public_duration'])
        for e in self.data['entries']:
            overlaps = sum(max(0, min(end, e['end'])-max(start, e['start'])) for start, end in ranges)
            self.assertAlmostEqual(overlaps, e['end']-e['start'] if e['channel'] == 'public' else 0)

    def sample(self):
        """The regression sample for this game, matched on its opening event."""
        with open(self.game / 'events.jsonl', encoding='utf-8') as f:
            opening = json.loads(f.readline())['hash']
        if opening not in SAMPLES: self.skipTest('没有这局的回归样例')
        return SAMPLES[opening]

    def test_sample_authoritative_states(self):
        sample, b = self.sample(), self.by_id
        # Each death shows on the line that announces it: the host's death or vote
        # result, or the narrated self-explosion.
        for before, after, seats in sample['deaths']:
            with self.subTest(announcement=after):
                for seat in seats:
                    self.assertIn(seat, b[before]['state']['alive'])
                    self.assertNotIn(seat, b[after]['state']['alive'])
        for before, after, old, new in sample['sheriff']:
            with self.subTest(badge=after):
                self.assertEqual(b[before]['state']['sheriff'], old)
                self.assertEqual(b[after]['state']['sheriff'], new)
        before, after, winner = sample['winner']
        self.assertIsNone(b[before]['state']['winner'])
        self.assertEqual(b[after]['state']['winner'], winner)
        self.assertEqual(self.data['entries'][-1]['state']['alive'], sample['survivors'])

    def test_sample_sheriff_race_marks_the_candidates_until_the_election(self):
        sample, b = self.sample(), self.by_id
        for line, candidates in sample['candidates']:
            with self.subTest(line=line):
                self.assertEqual(b[line]['state']['candidates'], candidates)

    def test_identity_and_explicit_model(self):
        players = self.data['players']
        self.assertEqual(len(players), 12)
        self.assertEqual(sum(p['role'] == '狼人' for p in players), 4)
        try: sample = self.sample()
        except unittest.SkipTest: return
        self.assertEqual([p['role'] for p in players], sample['roles'])
        self.assertTrue(all(p['model'] == 'luna' for p in players))

    def test_inputs_unchanged(self):
        self.assertEqual(self.before, hashes(self.game))

    def test_unknown_model_is_not_guessed(self):
        seats = read(self.game / 'seats.json')
        seats.pop('all_seats_model', None)
        seats.pop('all_seats_provider', None)
        seats['seats']['p01'].pop('model', None)
        seats['seats']['p01'].pop('provider', None)
        with patch('build.read', side_effect=lambda path: seats if path.name == 'seats.json' else read(path)):
            player = load_game(self.game)['players'][0]
        self.assertEqual(player['model'], '未知')
        self.assertEqual(player['provider'], '未知')

    def test_chat_seq_cannot_change_event_mapping(self):
        transcript = read(self.game / 'transcript.json')
        chat = next(e for e in transcript['entries'] if e['source']['type'] == 'chat')
        chat['source'].update(seq=37, kind='sheriff_elected')
        with patch('build.read', side_effect=lambda path: transcript if path.name == 'transcript.json' else read(path)):
            self.assertEqual(load_game(self.game), self.data)

    def test_frame_mapping_when_built(self):
        path = self.game / 'replay/frames.json'
        if not path.exists(): self.skipTest('MP4 尚未生成')
        frames = read(path)
        pages = frame_cues(self.data)
        self.assertEqual(len(frames), len(pages))
        for i, (frame, (e, cue)) in enumerate(zip(frames, pages)):
            self.assertEqual((frame['id'], frame['page']), (e['id'], cue['index']))
            self.assertEqual(frame['start'], cue['start'])
            self.assertEqual(frame['speaker'], e['speaker'])
            self.assertEqual(frame['state'], e['state'])
            self.assertEqual(frame['subtitle_sha256'], hashlib.sha256(cue['text'].encode()).hexdigest())
            if 'rendered' in frame:
                rendered = frame['rendered']
                self.assertEqual(rendered['renderer'], 'Pillow')
                self.assertEqual(rendered['size'], [1280, 720])
                self.assertEqual(''.join(rendered['subtitle_lines']), cue['text'])
                for seat, player in zip(rendered['seats'], self.data['players']):
                    self.assertEqual(seat['role'], player['role'])
                    self.assertEqual(seat['active'], e['speaker'] == player['id'])
                    self.assertEqual(seat['dead'], player['id'] not in e['state']['alive'])
                    self.assertEqual(seat['sheriff'], e['state']['sheriff'] == player['id'])
                    self.assertEqual(seat['candidate'], player['id'] in e['state']['candidates'])
                    self.assertEqual(seat['model'], player['model_label'] if e['state']['winner'] else '')
            if i: self.assertEqual(frames[i-1]['end'], frame['start'])
        self.assertGreaterEqual(frames[-1]['end'], self.data['duration'])

    def test_subtitle_pages_fit_and_keep_every_character_in_time(self):
        from render import SUBTITLE_LINES, SUBTITLE_SIZE, SUBTITLE_WIDTH, cues, font, wrap
        for e in self.data['entries']:
            pages = cues(e)
            self.assertEqual(''.join(c['text'] for c in pages), e['text'].replace('\n', ''))
            self.assertEqual(pages[0]['start'], e['start'])
            self.assertEqual(pages[-1]['end'], e['end'])
            for before, after in zip(pages, pages[1:]):
                self.assertEqual(before['end'], after['start'])
                self.assertLess(before['start'], before['end'])
            for c in pages:
                lines = wrap(c['text'], SUBTITLE_SIZE, SUBTITLE_WIDTH)
                self.assertLessEqual(len(lines), SUBTITLE_LINES)
                # Only hanging punctuation may pass the width.
                self.assertTrue(all(font(SUBTITLE_SIZE).getlength(line[:-1]) <= SUBTITLE_WIDTH for line in lines))
                self.assertTrue(all(not line or line[0] not in '，。；：、！？' for line in lines[1:]))

    def test_pillow_draws_authoritative_state(self):
        from PIL import Image
        from render import cues, render_frame
        with tempfile.TemporaryDirectory() as directory:
            for e in [next(e for e in self.data['entries'] if e['channel'] == 'wolves'),
                      next(e for e in self.data['entries'] if len(e['state']['alive']) < 12),
                      self.data['entries'][-1]]:
                path = Path(directory) / 'frame.png'
                rendered = render_frame(self.data, e, path, cues(e)[0])
                with Image.open(path) as image:
                    self.assertEqual(image.size, (1280, 720))
                for seat, player in zip(rendered['seats'], self.data['players']):
                    self.assertEqual(seat['active'], e['speaker'] == player['id'])
                    self.assertEqual(seat['dead'], player['id'] not in e['state']['alive'])
                    self.assertEqual(seat['sheriff'], e['state']['sheriff'] == player['id'])
                    self.assertEqual(seat['candidate'], player['id'] in e['state']['candidates'])
                    self.assertEqual(seat['role'], player['role'])
                    self.assertEqual(bool(seat['model']), bool(e['state']['winner']))

    def test_decoded_video_rejects_stale_subtitle(self):
        from render import cues, render_frame
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            expected = max(self.data['entries'], key=lambda e: len(e['text']))
            right, wrong = cues(expected)[:2]
            render_frame(self.data, expected, temp / 'frame-0000.png', right)
            render_frame(self.data, expected, temp / 'wrong.png', wrong)
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
                            '-i', str(temp / 'wrong.png'), '-frames:v', '1',
                            '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                            str(temp / 'god.mp4')], check=True)
            with self.assertRaisesRegex(ValueError, '视频帧与源图差异过大'):
                verify_frame_images(temp, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
