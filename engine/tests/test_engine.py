import concurrent.futures
import json
import re
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from engine.server import Game, ROLES, RuleError, SEATS, Server, Store

SCRIPT = Path(__file__).resolve().parents[1] / 'server.py'


class Client:
    """Real independent stdio process, with an environment-locked identity."""

    def __init__(self, root, seat, **extra):
        env = {**os.environ, 'WW_GAMES_DIR': str(root), **extra}
        if seat is None:
            env.pop('WW_SEAT', None)
        else:
            env['WW_SEAT'] = seat
        self.process = subprocess.Popen([sys.executable, str(SCRIPT)], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        self.seq = 0

    def rpc(self, method, params=None):
        self.seq += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.seq,
                                            'method': method, 'params': params or {}}) + '\n')
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], 10)
        if not ready:
            raise AssertionError('MCP server response timeout')
        response = json.loads(self.process.stdout.readline())
        if 'error' in response:
            raise AssertionError(response)
        return response['result']

    def tool(self, name, args=None, error=False):
        result = self.rpc('tools/call', {'name': name, 'arguments': args or {}})
        if result['isError'] != error:
            raise AssertionError((name, result))
        text = result['content'][0]['text']
        return text if error else json.loads(text)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()


class Contract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'round-1'
        self.directory.mkdir(mode=0o700)
        (self.root / 'current').symlink_to('round-1')
        self.store = Store(self.root)
        self.call('god', 'start_game', seed=7)

    def call(self, actor, action, **args):
        return self.store.call(actor, action, args)

    def game(self):
        with self.store.locked() as (_, directory_fd, _):
            return self.store.load(directory_fd)[0]

    def role(self, role):
        return self.game().role_seat(role)

    def players(self, role):
        return [p for p, r in self.game().roles.items() if r == role]

    def night(self, guard=None, kill=None, mode='pass', poison=None):
        g = self.game()
        if g.phase == 'night_guard':
            self.call(self.role('guard'), 'guard', target=guard)
        g = self.game()
        if g.phase == 'night_wolves':
            wolf = next(p for p in g.living() if g.roles[p] == 'wolf')
            self.call(wolf, 'wolf_kill', target=kill)
        g = self.game()
        if g.phase == 'night_witch':
            self.call(self.role('witch'), 'witch', mode=mode, target=poison)
        g = self.game()
        if g.phase == 'night_seer':
            self.call(self.role('seer'), 'inspect', target=g.living()[0])

    def join(self, candidates=()):
        for p in SEATS:
            self.call(p, 'sheriff_join', join=p in candidates)

    def speech(self):
        while self.game().phase.endswith('_speech'):
            self.call(self.game().speakers[0], 'speak', text='这是我的发言。')

    def effects(self):
        while self.game().phase in ('shoot', 'badge', 'last_words'):
            g = self.game()
            action, actor = g.effects[0]
            self.call(actor, action, **({'text': '我的遗言。'} if action == 'last_words' else {'target': None}))

    def day_vote(self):
        if self.game().phase == 'sheriff_join':
            self.join()
        if self.game().phase == 'announce':
            self.call('god', 'advance')
        self.effects()
        if self.game().phase == 'choose_order':
            self.call(self.game().sheriff, 'choose_order', direction='left')
        self.speech()
        self.assertEqual(self.game().phase, 'vote')

    def votes(self, mapping=None):
        mapping = mapping or {}
        for p in self.game().vote_state['voters']:
            self.call(p, 'vote', target=mapping.get(p))
        self.assertEqual(self.game().phase, 'vote_result')

    def client(self, seat, **env):
        client = Client(self.root, seat, **env)
        self.addCleanup(client.close)
        return client

    def test_every_host_line_is_ready_to_read_aloud_through_a_whole_game(self):
        lines = []
        for _ in range(500):
            g = self.game()
            for call in g.next_step({})['calls']:
                self.assert_host_line(call['say'])
                lines.append(call['say'])
            phase = g.phase
            if phase == 'finished':
                break
            if phase.startswith('night_'):
                good = next(p for p in g.living() if g.roles[p] != 'wolf')
                self.night(kill=good)
            elif phase == 'sheriff_join':
                self.join(SEATS[:2])
            elif phase.endswith('_speech'):
                self.speech()
            elif phase == 'withdraw':
                for p in g.candidates:
                    if p not in g.withdrawals:
                        self.call(p, 'withdraw', withdraw=False)
            elif phase == 'vote':
                target = g.vote_state['candidates'][0]
                for p in g.vote_state['voters']:
                    self.call(p, 'vote', target=target)
            elif phase in ('announce', 'vote_result'):
                self.call('god', 'advance')
            elif phase == 'choose_order':
                self.call(g.sheriff, 'choose_order', direction='left')
            else:
                self.effects()
        self.assertEqual(self.game().phase, 'finished')
        self.assertIn('阵营获胜', lines[-1])
        self.assertGreaterEqual(lines.count('天黑请闭眼。'), 2)
        self.assertTrue(any(line.startswith('投票结果') for line in lines))

    def test_seed_and_role_distribution(self):
        g = self.game()
        self.assertCountEqual(g.roles.values(), ROLES)
        h = Game()
        h.apply('god', 'start_game', {'seed': 7})
        self.assertEqual(g.roles, h.roles)
        events = [json.loads(s) for s in (self.directory / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[0]['args'], {'seed': 7})
        with self.assertRaises(RuleError):
            self.call('god', 'start_game', seed=8)

    def test_same_guard_and_save_kills(self):
        victim = self.players('villager')[0]
        self.night(guard=victim, kill=victim, mode='save')
        # Death not leaked before the sheriff election.
        for p in SEATS:
            self.assertIn(victim, self.call(p, 'my_info')['alive'])
        self.join()
        self.call('god', 'advance')
        self.assertNotIn(victim, self.game().alive)
        news = [e for e in self.call('p01', 'my_info')['history'] if e['kind'] == 'night_deaths'][-1]
        self.assertEqual(news['players'], [victim])
        self.assertNotIn('cause', news)
        self.assertEqual(self.game().phase, 'last_words')

    def test_guard_protects_and_cannot_repeat(self):
        victim = self.role('guard')
        self.night(guard=victim, kill=victim)
        self.day_vote()
        self.assertIn(victim, self.game().alive)
        self.votes()
        self.call('god', 'advance')
        with self.assertRaisesRegex(RuleError, '连续'):
            self.call(victim, 'guard', target=victim)
        self.call(victim, 'guard', target=None)

    def test_witch_no_self_save_and_one_potion_per_night(self):
        witch = self.role('witch')
        self.call(self.role('guard'), 'guard', target=None)
        self.call(self.players('wolf')[0], 'wolf_kill', target=witch)
        self.assertEqual(self.call(witch, 'my_info')['knife_target'], witch)
        with self.assertRaisesRegex(RuleError, '自救'):
            self.call(witch, 'witch', mode='save', target=None)
        with self.assertRaises(RuleError):
            self.call(witch, 'witch', mode='save', target=self.role('hunter'))
        self.call(witch, 'witch', mode='poison', target=self.players('wolf')[0])
        with self.assertRaises(RuleError):
            self.call(witch, 'witch', mode='save', target=None)

    def test_witch_used_antidote_hides_later_knife(self):
        victim = self.players('villager')[0]
        witch = self.role('witch')
        self.night(kill=victim, mode='save')
        self.day_vote()
        self.votes()
        self.call('god', 'advance')
        self.call(self.role('guard'), 'guard', target=None)
        self.call(self.players('wolf')[0], 'wolf_kill', target=victim)
        self.assertNotIn('knife_target', self.call(witch, 'my_info'))
        with self.assertRaises(RuleError):
            self.call(witch, 'witch', mode='save', target=None)
        self.call(witch, 'witch', mode='poison', target=victim)
        self.call(self.role('seer'), 'inspect', target=victim)
        self.call('god', 'advance')
        self.assertNotIn(victim, self.game().alive)
        self.assertNotEqual(self.game().phase, 'last_words')

    def test_poisoned_hunter_cannot_shoot_even_if_guarded_and_knifed(self):
        hunter = self.role('hunter')
        self.night(guard=hunter, kill=hunter, mode='poison', poison=hunter)
        self.join()
        self.call('god', 'advance')
        self.assertNotIn('shoot', self.game().available(hunter))
        with self.assertRaises(RuleError):
            self.call(hunter, 'shoot', target=self.players('wolf')[0])
        self.assertEqual(self.game().phase, 'last_words')

    def test_knifed_hunter_can_shoot_once(self):
        hunter = self.role('hunter')
        wolf = self.players('wolf')[0]
        self.night(kill=hunter)
        self.join()
        self.call('god', 'advance')
        self.assertEqual(self.game().phase, 'shoot')
        self.call(hunter, 'shoot', target=wolf)
        self.assertNotIn(wolf, self.game().alive)
        with self.assertRaises(RuleError):
            self.call(hunter, 'shoot', target=self.players('wolf')[1])
        self.assertEqual(self.game().phase, 'last_words')

    def test_sheriff_withdraw_and_two_ties(self):
        self.night()
        candidates = list(SEATS[:3])
        self.join(candidates)
        self.speech()
        for p in candidates:
            self.call(p, 'withdraw', withdraw=p == candidates[2])
        self.assertEqual(self.game().vote_state['candidates'], candidates[:2])
        with self.assertRaises(RuleError):
            self.call(candidates[2], 'vote', target=candidates[0])
        voters = self.game().vote_state['voters']
        with self.assertRaises(RuleError):
            self.call(voters[0], 'vote', target=candidates[2])
        mapping = {p: candidates[i % 2] for i, p in enumerate(voters[:8])}
        self.votes(mapping)
        self.call('god', 'advance')
        self.assertEqual(self.game().phase, 'sheriff_pk_speech')
        self.speech()
        self.assertEqual(self.game().vote_state['round'], 2)
        self.votes(mapping)
        self.call('god', 'advance')
        self.assertIsNone(self.game().sheriff)
        self.assertEqual(self.game().phase, 'announce')

    def test_single_all_no_candidates_and_all_withdraw(self):
        # Independent deterministic projections, each populated with real valid actions.
        for candidates in ([], [SEATS[0]], list(SEATS), list(SEATS[:2])):
            with self.subTest(candidates=candidates):
                game = Game()
                game.apply('god', 'start_game', {'seed': 7})
                for role, action, args in [('guard', 'guard', {'target': None}),
                                          ('wolf', 'wolf_kill', {'target': None}),
                                          ('witch', 'witch', {'mode': 'pass', 'target': None}),
                                          ('seer', 'inspect', {'target': SEATS[0]})]:
                    game.apply(game.role_seat(role), action, args)
                for p in SEATS:
                    game.apply(p, 'sheriff_join', {'join': p in candidates})
                if len(candidates) == 2:
                    while game.phase == 'sheriff_speech':
                        game.apply(game.speakers[0], 'speak', {'text': '发言'})
                    for p in candidates:
                        game.apply(p, 'withdraw', {'withdraw': True})
                self.assertEqual(game.phase, 'announce')
                self.assertEqual(game.sheriff, SEATS[0] if len(candidates) == 1 else None)

    def test_exile_pk_and_second_tie(self):
        self.night()
        self.day_vote()
        tied = list(SEATS[:2])
        self.votes({p: tied[i % 2] for i, p in enumerate(SEATS)})
        self.call('god', 'advance')
        self.assertEqual(self.game().phase, 'exile_pk_speech')
        self.speech()
        voters = self.game().vote_state['voters']
        self.assertTrue(all(p not in voters for p in tied))
        for p in tied:
            with self.assertRaises(RuleError):
                self.call(p, 'vote', target=tied[0])
        self.votes({p: tied[i % 2] for i, p in enumerate(voters)})
        self.call('god', 'advance')
        self.assertEqual(len(self.game().alive), 12)
        self.assertEqual(self.game().phase, 'night_guard')

    def test_sheriff_weight_direction_exiled_hunter_and_badge(self):
        hunter = self.role('hunter')
        wolf = self.players('wolf')[0]
        self.night()
        self.join([hunter])
        self.call('god', 'advance')
        self.call(hunter, 'choose_order', direction='right')
        self.assertEqual(self.game().speakers[-1], hunter)
        self.speech()
        # Two votes (including sheriff) beat two ordinary votes: 2.5 versus 2.
        voters = [p for p in SEATS if p != hunter]
        self.votes({hunter: hunter, voters[0]: hunter, voters[1]: wolf, voters[2]: wolf})
        result = self.call('god', 'next_step')['vote_result']
        self.assertEqual(result['totals'][hunter], 2.5)
        self.assertEqual(result['totals'][wolf], 2)
        self.call('god', 'advance')
        self.assertEqual(self.game().phase, 'shoot')
        self.call(hunter, 'shoot', target=None)
        self.assertEqual(self.game().phase, 'badge')
        self.call(hunter, 'badge', target=wolf)
        self.assertEqual(self.game().sheriff, wolf)
        self.call(hunter, 'last_words', text='遗言')
        self.assertEqual(self.game().phase, 'night_guard')

    def test_explosion_only_own_turn_campaign_cancels_badge(self):
        wolf = self.players('wolf')[0]
        victim = self.players('villager')[0]
        self.night(kill=victim)
        self.join([wolf, victim])
        with self.assertRaises(RuleError):
            self.call(self.players('wolf')[1], 'explode')
        while self.game().speakers[0] != wolf:
            self.call(self.game().speakers[0], 'speak', text='发言')
        self.call(wolf, 'explode')
        g = self.game()
        self.assertNotIn(wolf, g.alive)
        self.assertNotIn(victim, g.alive)
        self.assertTrue(g.badge_lost)
        self.assertIsNone(g.sheriff)
        self.assertEqual(g.day, 1)
        self.assertEqual(g.phase, 'last_words')
        self.assertEqual(g.pending_deaths, [])
        self.assertEqual(g.effects, [('last_words', victim)])
        self.assertEqual(g.history[-1], {'kind': 'night_deaths', 'day': 1, 'players': [victim]})
        step = self.client('god').tool('next_step')
        self.assertEqual(step['calls'][0]['actors'], [victim])
        self.assertEqual(step['calls'][0]['actions'], ['last_words'])
        self.assertEqual(step['calls'][0]['target'], 'public')
        self.client(wolf).tool('last_words', {'text': '自爆无遗言'}, error=True)
        self.client('god').tool('advance', error=True)
        player = self.client(victim)
        player.tool('last_words', {'text': '首夜遗言'})
        player.tool('last_words', {'text': '不得重复遗言'}, error=True)
        g = self.game()
        self.assertEqual((g.day, g.phase, g.effects), (2, 'night_guard', []))
        self.assertEqual([e['kind'] for e in g.history[-3:]], ['night_deaths', 'last_words', 'night_started'])

    def test_regular_sheriff_explosion_transfers_badge_then_night(self):
        wolf = self.players('wolf')[0]
        self.night()
        self.join([wolf])
        self.call('god', 'advance')
        self.call(wolf, 'choose_order', direction='left')
        while self.game().speakers[0] != wolf:
            self.call(self.game().speakers[0], 'speak', text='发言')
        self.call(wolf, 'explode')
        self.assertEqual(self.game().phase, 'badge')
        self.call(wolf, 'badge', target=None)
        self.assertEqual(self.game().phase, 'night_guard')
        self.assertFalse(any(e['kind'] == 'last_words' and e['player'] == wolf for e in self.game().history))

    def test_wolves_win_by_villager_edge_and_export_replay(self):
        for victim in self.players('villager'):
            self.night(kill=victim)
            if self.game().phase == 'sheriff_join':
                self.join()
            self.call('god', 'advance')
            if self.game().winner:
                break
            self.day_vote()
            self.votes()
            self.call('god', 'advance')
        self.assertEqual(self.game().winner, 'wolves')
        self.assertEqual(self.game().phase, 'finished')
        export = self.client('god').tool('export_replay')
        self.assertTrue(any(e['action'] == 'wolf_kill' for e in export['events']))
        replay = self.root / 'copy'
        replay.mkdir(mode=0o700)
        log = replay / 'events.jsonl'
        log.write_text(export['jsonl'])
        log.chmod(0o600)
        fd = os.open(replay, os.O_RDONLY | os.O_DIRECTORY)
        try:
            restored, events, raw = self.store.load(fd)
        finally:
            os.close(fd)
        self.assertEqual(restored.info('god'), self.game().info('god'))
        self.assertEqual(events, export['events'])
        self.assertEqual(raw.decode(), export['jsonl'])
        self.client('p01').tool('export_replay', error=True)
        with self.assertRaises(RuleError):
            self.call(self.players('wolf')[0], 'wolf_kill', target=None)
        # A finished game makes way for the next one without asking.
        self.assertEqual(self.call('lead', 'new_game', models=self.models(), abandon_current=False)['game_id'], 'g001')

    def test_wolves_win_by_god_edge(self):
        # Kill seer, witch, guard, then hunter; victory precedes hunter's shot.
        for role in ('seer', 'witch', 'guard', 'hunter'):
            self.night(kill=self.role(role))
            if self.game().phase == 'sheriff_join':
                self.join()
            self.call('god', 'advance')
            if self.game().winner:
                break
            self.day_vote()
            self.votes()
            self.call('god', 'advance')
        self.assertEqual(self.game().winner, 'wolves')
        self.assertNotIn('shoot', self.game().available(self.role('hunter')))

    def test_good_win_and_last_wolf_explosion(self):
        wolves = self.players('wolf')
        for victim in wolves[:-1]:
            self.night()
            self.day_vote()
            self.votes({p: victim for p in self.game().living()})
            self.call('god', 'advance')
            self.effects()
        self.night()
        self.call('god', 'advance')
        while self.game().speakers[0] != wolves[-1]:
            self.call(self.game().speakers[0], 'speak', text='发言')
        self.call(wolves[-1], 'explode')
        self.assertEqual(self.game().winner, 'good')
        self.assertEqual(self.game().phase, 'finished')

    def test_phase_action_permissions_do_not_append_invalid_actions(self):
        log = self.directory / 'events.jsonl'
        before = log.read_bytes()
        for actor, action, args in [('god', 'guard', {'target': None}),
                                    (self.players('villager')[0], 'guard', {'target': None}),
                                    (self.role('guard'), 'vote', {'target': None}),
                                    ('god', 'advance', {}), ('p01', 'next_step', {}),
                                    ('p01', 'my_info', {'seat': 'p02'}),
                                    ('p01', 'note', {'text': 'x', 'role': 'god'}),
                                    ('god', 'export_replay', {})]:
            with self.subTest(actor=actor, action=action), self.assertRaises(RuleError):
                self.call(actor, action, **args)
        self.assertEqual(before, log.read_bytes())

    def test_stdio_initialize_negotiates_supported_versions(self):
        for version in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
            with self.subTest(version=version):
                client = self.client('god')
                result = client.rpc('initialize', {'protocolVersion': version, 'capabilities': {},
                                                  'clientInfo': {'name': 'contract', 'version': '1'}})
                self.assertEqual(result['protocolVersion'], version)
                self.assertIn('tools', result['capabilities'])
                self.assertEqual(client.rpc('ping'), {})

    def test_stdio_initialize_unsupported_version_keeps_fallback(self):
        client = self.client('god')
        result = client.rpc('initialize', {'protocolVersion': '2099-01-01', 'capabilities': {},
                                          'clientInfo': {'name': 'contract', 'version': '1'}})
        self.assertEqual(result['protocolVersion'], '2024-11-05')

    def test_all_13_stdio_identities_private_fields_and_notes(self):
        god = self.client('god')
        init = god.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                      'clientInfo': {'name': 'contract', 'version': '1'}})
        self.assertEqual(init['protocolVersion'], '2025-03-26')
        self.assertIn('tools', init['capabilities'])
        self.assertEqual(god.rpc('ping'), {})
        roles = god.tool('my_info')['roles']
        clients = {p: self.client(p) for p in SEATS}
        for p, client in clients.items():
            client.tool('note', {'text': f'private-{p}'})
        for p, client in clients.items():
            with self.subTest(seat=p, role=roles[p]):
                info = client.tool('my_info')
                self.assertEqual(info['self'], p)
                self.assertEqual(info['role'], roles[p])
                self.assertEqual([n['text'] for n in info['notes']], [f'private-{p}'])
                self.assertNotIn('roles', info)
                self.assertNotIn('seed', info)
                self.assertNotIn('events', info)
                if roles[p] == 'wolf':
                    self.assertEqual(set(info['teammates']), {s for s, r in roles.items() if r == 'wolf'})
                else:
                    self.assertNotIn('teammates', info)
                for field, role in [('potions', 'witch'), ('last_guard', 'guard'), ('inspections', 'seer')]:
                    self.assertEqual(field in info, roles[p] == role)
                client.tool('my_info', {'seat': 'god'}, error=True)
                client.tool('my_info', {'player': SEATS[(SEATS.index(p) + 1) % 12]}, error=True)
                client.tool('note', {'text': 'spoof', 'token': 'god'}, error=True)
                client.tool('next_step', error=True)
                client.tool('export_replay', error=True)
                client.tool('start_game', {'seed': 1}, error=True)
                names = {t['name'] for t in client.rpc('tools/list')['tools']}
                self.assertNotIn('advance', names)
                self.assertNotIn('next_step', names)
                self.assertNotIn('export_replay', names)
        god.tool('note', {'text': '冒充'}, error=True)
        god.tool('guard', {'target': None}, error=True)
        god.tool('my_info', {'WW_SEAT': 'p01'}, error=True)
        self.assertNotIn('notes', god.tool('my_info'))
        clients[self.role('guard')].tool('guard', {'target': None})
        knife = self.players('villager')[0]
        clients[self.players('wolf')[0]].tool('wolf_kill', {'target': knife})
        for p, client in clients.items():
            info = client.tool('my_info')
            self.assertEqual('knife_target' in info, roles[p] == 'witch')
        clients[self.role('witch')].tool('witch', {'mode': 'pass', 'target': None})
        clients[self.role('seer')].tool('inspect', {'target': self.players('wolf')[0]})
        inspection = clients[self.role('seer')].tool('my_info')['inspections'][0]
        self.assertEqual(inspection['alignment'], 'wolf')
        self.assertNotIn('role', inspection)
        for p, client in clients.items():
            if roles[p] != 'seer':
                self.assertNotIn('inspections', client.tool('my_info'))

    def test_missing_unknown_identity_and_environment_locked_at_boot(self):
        for seat in (None, '', 'p00', 'p13', 'God', 'p01 ', 'wolf'):
            with self.subTest(seat=seat):
                client = self.client(seat)
                self.assertEqual(client.process.wait(timeout=5), 2)
                self.assertIn('WW_SEAT', client.process.stderr.read())
        old = os.environ.get('WW_SEAT')
        try:
            os.environ['WW_SEAT'] = 'p01'
            server = Server()
            os.environ['WW_SEAT'] = 'god'
            self.assertEqual(server.actor, 'p01')
        finally:
            if old is None:
                os.environ.pop('WW_SEAT', None)
            else:
                os.environ['WW_SEAT'] = old

    def test_secret_voting_real_processes_until_every_vote_arrives(self):
        self.night()
        self.day_vote()
        god = self.client('god')
        clients = {p: self.client(p) for p in SEATS}
        before_god_info = god.tool('my_info')
        before_god_next = god.tool('next_step')
        self.assert_public_vote_call(before_god_next, list(SEATS))
        before_player = {p: c.tool('my_info') for p, c in clients.items()}
        for p in SEATS[:-1]:
            self.assertEqual(clients[p].tool('vote', {'target': SEATS[0]}), {'accepted': True})
            self.assertEqual(god.tool('my_info'), before_god_info)
            self.assertEqual(god.tool('next_step'), before_god_next)
            for other in (p, SEATS[-1]):
                self.assertEqual(clients[other].tool('my_info'), before_player[other])
            god.tool('export_replay', error=True)
            god.tool('advance', error=True)
        clients[SEATS[-1]].tool('vote', {'target': None})
        result = god.tool('next_step')['vote_result']
        self.assertEqual(len(result['ballots']), 12)
        self.assertEqual(result['totals'][SEATS[0]], 11)
        self.assertNotIn('ballots', clients[SEATS[1]].tool('my_info')['voting'])
        god.tool('advance')
        public = clients[SEATS[1]].tool('my_info')['history']
        self.assertEqual([e for e in public if e['kind'] == 'vote_result'][-1]['ballots'], result['ballots'])

    def test_concurrent_process_submissions_and_duplicate_vote(self):
        clients = {p: self.client(p) for p in SEATS}
        def notes(item):
            p, client = item
            for i in range(8):
                client.tool('note', {'text': f'{p}:{i}'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(notes, clients.items()))
        g = self.game()
        for p in SEATS:
            self.assertEqual([n['text'] for n in g.notes[p]], [f'{p}:{i}' for i in range(8)])
        self.night()
        self.day_vote()
        duplicate = self.client(SEATS[0])
        def vote(client):
            return client.rpc('tools/call', {'name': 'vote', 'arguments': {'target': None}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=13) as executor:
            results = list(executor.map(vote, [*clients.values(), duplicate]))
        self.assertEqual(sum(not r['isError'] for r in results), 12)
        self.assertEqual(self.game().phase, 'vote_result')
        with self.store.locked() as (_, fd, _):
            _, events, raw = self.store.load(fd)
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(e['action'] == 'note' for e in events), 96)
        self.assertEqual(sum(e['action'] == 'vote' for e in events), 12)
        self.assertEqual(len(raw.splitlines()), len(events))

    def test_next_step_mapping_and_no_fabricated_conversation_ids(self):
        guard = self.role('guard')
        step = self.call('god', 'next_step')
        # Nightfall is announced in public before the first role wakes.
        self.assertEqual(step['calls'][0]['target'], 'public')
        self.assertEqual(step['calls'][0]['say'], '天黑请闭眼。')
        self.assertEqual(step['calls'][1]['actors'], [guard])
        self.assertEqual(step['calls'][1]['target'], f'private:{guard}')
        self.assertIsNone(step['calls'][1]['conversation_id'])
        god = self.client('god', WW_CONVERSATIONS=json.dumps({f'private:{guard}': 'configured-private-id'}))
        mapped = god.tool('next_step')['calls'][1]
        self.assertEqual(mapped['conversation_id'], 'configured-private-id')
        self.assertFalse(mapped['mapping_required'])
        self.assertEqual(step['calls'][0]['mentions'], [])
        self.assertEqual(step['calls'][1]['mentions'], [guard])
        # Member ids come from the game's seats.json; seats without one stay as they are.
        (self.directory / 'seats.json').write_text(json.dumps(
            {'seats': {'god': {'model': 'luna'}, guard: {'model': 'luna', 'principal': f'g009-{guard}'}}}))
        self.assertEqual(god.tool('next_step')['calls'][1]['mentions'], [f'g009-{guard}'])
        (self.directory / 'seats.json').write_text('{"seats": {"%s": {"principal": ""}}}' % guard)
        god.tool('next_step', error=True)

    def assert_public_vote_call(self, step, voters, conversation_id=None):
        self.assertEqual(step['phase'], 'vote')
        self.assertEqual(set(step), {'day', 'phase', 'calls', 'winner'})
        # A sheriff vote opens with the host's notice of who stayed in the race.
        *notices, call = step['calls']
        self.assertTrue(all(n['actions'] == [] and n['target'] == 'public' for n in notices))
        self.assertLessEqual(len(notices), 1)
        self.assertEqual(set(call), {'actors', 'target', 'conversation_id', 'mapping_required',
                                     'actions', 'say', 'instruction', 'voters', 'mentions', 'key'})
        self.assertEqual(call['target'], 'public')
        self.assertEqual(call['conversation_id'], conversation_id)
        self.assertEqual(call['mapping_required'], conversation_id is None)
        for field in ('actors', 'voters', 'mentions'):
            self.assertEqual(call[field], voters)
        self.assertEqual(call['actions'], ['vote'])
        self.assertIn('点名', call['instruction'])
        self.assertIn('已投票', call['instruction'])
        self.assertIn('私聊', call['instruction'])
        self.assertNotIn('[[silent]]', call['instruction'])
        self.assert_host_line(call['say'])
        self.assertIn('投票', call['say'])

    def assert_host_line(self, line):
        # Host lines are read aloud to viewers: no tool talk, no engine IDs.
        self.assertTrue(line)
        self.assertIsNone(re.search(r'调用|工具|提交|引擎|MCP|next_step|my_info|advance|null|p\d\d|\[\[', line), line)

    def test_public_vote_call_sheriff_withdrawal_and_pk_mapping(self):
        candidates = list(SEATS[:3])
        self.night()
        self.join(candidates)
        self.speech()
        for p in candidates:
            self.call(p, 'withdraw', withdraw=p == candidates[-1])
        voters = list(SEATS[3:])
        god = self.client('god', WW_CONVERSATIONS=json.dumps({'public': 'configured-public-id'}))
        missing = self.client('god', WW_CONVERSATIONS=json.dumps({'private:p04': 'private-id'}))
        for round_number in (1, 2):
            before = god.tool('next_step')
            self.assert_public_vote_call(before, voters, 'configured-public-id')
            self.assert_public_vote_call(missing.tool('next_step'), voters)
            for i, p in enumerate(voters):
                self.call(p, 'vote', target=candidates[i % 2] if i < 8 else None)
                if i < len(voters) - 1:
                    self.assertEqual(god.tool('next_step'), before)
            self.assertEqual(god.tool('next_step')['vote_result']['round'], round_number)
            self.call('god', 'advance')
            if round_number == 1:
                self.speech()
        self.assertIsNone(self.game().sheriff)

    def test_public_vote_call_exile_pk_excludes_dead_and_pk_seats(self):
        victim = self.players('villager')[0]
        self.night(kill=victim)
        self.day_vote()
        voters = self.game().living()
        self.assert_public_vote_call(self.call('god', 'next_step'), voters)
        candidates = voters[:2]
        self.votes({p: candidates[i % 2] for i, p in enumerate(voters[:10])})
        self.call('god', 'advance')
        self.speech()
        voters = [p for p in voters if p not in candidates]
        before = self.call('god', 'next_step')
        self.assert_public_vote_call(before, voters)
        self.call(voters[0], 'vote', target=None)
        self.assertEqual(self.call('god', 'next_step'), before)

    def test_corruption_truncated_log_and_symlinks_fail_closed(self):
        log = self.directory / 'events.jsonl'
        original = log.read_bytes()
        for damaged in (original[:-1], original.replace(b'"seed":7', b'"seed":8'), original + b'{bad}\n'):
            with self.subTest(damaged=damaged[-20:]):
                log.write_bytes(damaged)
                with self.assertRaises(RuleError):
                    self.call('p01', 'note', text='cannot append')
                self.assertEqual(log.read_bytes(), damaged)
        log.write_bytes(original)
        moved = self.directory / 'original.jsonl'
        log.rename(moved)
        log.symlink_to(moved)
        with self.assertRaises(RuleError):
            self.call('p01', 'my_info')
        log.unlink()
        moved.rename(log)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to('../escape')
        with self.assertRaises(RuleError):
            self.call('p01', 'note', text='cannot escape')
        self.assertEqual(log.read_bytes(), original)

    def test_current_directory_missing_identity_and_log_permissions(self):
        current = self.root / 'current'
        current.unlink()
        current.mkdir()
        with self.assertRaises(RuleError):
            self.call('p01', 'my_info')
        current.rmdir()
        current.symlink_to('round-1')
        log = self.directory / 'events.jsonl'
        log.chmod(0o644)
        with self.assertRaises(RuleError):
            self.call('p01', 'my_info')
        log.chmod(0o600)
        self.assertEqual(self.call('p01', 'my_info')['self'], 'p01')

    def test_hidden_night_victim_can_win_election_then_transfer_badge(self):
        victim = self.players('villager')[0]
        other = next(p for p in SEATS if p != victim)
        self.night(kill=victim)
        self.join([victim, other])
        self.speech()
        for p in (victim, other):
            self.call(p, 'withdraw', withdraw=False)
        self.votes({p: victim for p in self.game().vote_state['voters']})
        self.call('god', 'advance')
        self.assertEqual(self.game().sheriff, victim)
        self.assertIn(victim, self.game().alive)
        self.call('god', 'advance')
        self.assertNotIn(victim, self.game().alive)
        self.assertEqual(self.game().phase, 'badge')
        self.call(victim, 'badge', target=other)
        self.call(victim, 'last_words', text='首夜遗言')
        self.assertEqual(self.game().phase, 'choose_order')

    def test_campaign_explosion_preserves_pending_hunter_shot(self):
        hunter = self.role('hunter')
        wolf = self.players('wolf')[0]
        self.night(kill=hunter)
        self.join([hunter, wolf])
        while self.game().speakers[0] != wolf:
            self.call(self.game().speakers[0], 'speak', text='发言')
        self.client(wolf).tool('explode')
        self.assertEqual(self.game().phase, 'shoot')
        god = self.client('god')
        step = god.tool('next_step')
        self.assertEqual(step['calls'][0]['actors'], [hunter])
        self.assertEqual(step['calls'][0]['actions'], ['shoot'])
        self.assertEqual(self.game().effects, [('shoot', hunter), ('last_words', hunter)])
        target = self.players('wolf')[1]
        player = self.client(hunter)
        player.tool('shoot', {'target': target})
        self.assertNotIn(target, self.game().alive)
        self.assertEqual(self.game().phase, 'last_words')
        self.assertEqual(god.tool('next_step')['calls'][0]['actions'], ['last_words'])
        player.tool('shoot', {'target': None}, error=True)
        player.tool('last_words', {'text': '开枪后首夜遗言'})
        g = self.game()
        self.assertEqual((g.day, g.phase, g.effects), (2, 'night_guard', []))
        self.assertEqual([e['kind'] for e in g.history[-4:]],
                         ['night_deaths', 'hunter_shot', 'last_words', 'night_started'])
        with self.store.locked() as (_, fd, _):
            restored, events, _ = self.store.load(fd)
        self.assertEqual(restored.info('god'), g.info('god'))
        self.assertEqual([e['action'] for e in events[-3:]], ['explode', 'shoot', 'last_words'])
        self.assertEqual(sum(e['kind'] == 'night_deaths' for e in restored.history), 1)

    def test_campaign_explosion_poisoned_hunter_and_other_victim_keep_words(self):
        hunter = self.role('hunter')
        victim = self.players('villager')[0]
        wolf = self.players('wolf')[0]
        self.night(kill=victim, mode='poison', poison=hunter)
        self.join([hunter, wolf])
        while self.game().speakers[0] != wolf:
            self.call(self.game().speakers[0], 'speak', text='发言')
        self.call(wolf, 'explode')
        victims = sorted([hunter, victim])
        self.assertEqual(self.game().effects, [('last_words', p) for p in victims])
        with self.assertRaises(RuleError):
            self.call(hunter, 'shoot', target=None)
        for p in victims:
            self.assertEqual(self.call('god', 'next_step')['calls'][0]['actors'], [p])
            self.call(p, 'last_words', text='首夜遗言')
        self.assertEqual((self.game().day, self.game().phase), (2, 'night_guard'))
        self.assertEqual([e['player'] for e in self.game().history if e['kind'] == 'last_words'], victims)

    def test_campaign_pk_explosion_without_night_deaths_enters_night(self):
        wolf = self.players('wolf')[0]
        other = self.players('villager')[0]
        self.night()
        self.join([wolf, other])
        self.speech()
        for p in (wolf, other):
            self.call(p, 'withdraw', withdraw=False)
        self.votes({p: [wolf, other][i % 2] for i, p in enumerate(self.game().electorate)})
        self.call('god', 'advance')
        self.assertEqual(self.game().phase, 'sheriff_pk_speech')
        while self.game().speakers[0] != wolf:
            self.call(self.game().speakers[0], 'speak', text='PK 发言')
        self.call(wolf, 'explode')
        g = self.game()
        self.assertEqual((g.day, g.phase, g.effects), (2, 'night_guard', []))
        self.assertTrue(g.badge_lost)
        self.assertIsNone(g.sheriff)
        self.assertEqual(g.history[-2]['kind'], 'night_deaths')
        self.assertEqual(g.history[-2]['players'], [])
        self.assertFalse(any(e['kind'] == 'last_words' for e in g.history))

    def test_campaign_exploding_pending_victim_dies_once_without_words(self):
        wolf = self.players('wolf')[0]
        self.night(kill=wolf)
        self.join([wolf, self.players('villager')[0]])
        while self.game().speakers[0] != wolf:
            self.call(self.game().speakers[0], 'speak', text='发言')
        self.call(wolf, 'explode')
        g = self.game()
        self.assertEqual(len(g.alive), 11)
        self.assertEqual(g.deaths, {wolf: 'explode'})
        self.assertEqual((g.day, g.phase, g.effects), (2, 'night_guard', []))
        self.assertEqual(g.history[-2]['players'], [wolf])
        with self.assertRaises(RuleError):
            self.call(wolf, 'last_words', text='自爆无遗言')

    def test_daytime_edge_victory_stops_pending_last_words(self):
        for victim in self.players('villager'):
            self.night()
            self.day_vote()
            self.votes({p: victim for p in self.game().living()})
            self.call('god', 'advance')
            if self.game().winner:
                break
            self.effects()
        self.assertEqual(self.game().winner, 'wolves')
        self.assertEqual(self.game().phase, 'finished')
        self.assertFalse(self.game().effects)
        with self.assertRaises(RuleError):
            self.call(victim, 'last_words', text='终局后不能继续')

    def test_dual_night_death_and_lowest_victim_speech_anchor(self):
        victims = self.players('villager')[:2]
        self.night(kill=victims[0], mode='poison', poison=victims[1])
        self.join()
        self.call('god', 'advance')
        self.assertTrue(all(p not in self.game().alive for p in victims))
        self.effects()
        anchor = SEATS.index(min(victims))
        order = [SEATS[(anchor + i) % 12] for i in range(1, 13)]
        self.assertEqual(self.game().speakers, [p for p in order if p not in victims])

    def test_empty_votes_self_inspection_and_dead_player_restrictions(self):
        seer = self.role('seer')
        self.call(self.role('guard'), 'guard', target=None)
        self.call(self.players('wolf')[0], 'wolf_kill', target=None)
        self.call(self.role('witch'), 'witch', mode='pass', target=None)
        self.call(seer, 'inspect', target=seer)
        self.assertEqual(self.call(seer, 'my_info')['inspections'][0]['alignment'], 'good')
        self.day_vote()
        self.votes()
        self.call('god', 'advance')
        self.assertEqual(len(self.game().alive), 12)
        victim = self.players('villager')[0]
        self.night(kill=victim)
        self.call('god', 'advance')
        self.assertEqual(self.game().phase, 'day_speech')
        for action, args in [('vote', {'target': None}), ('speak', {'text': '死亡发言'}),
                             ('last_words', {'text': '非首夜无遗言'}), ('explode', {})]:
            with self.assertRaises(RuleError):
                self.call(victim, action, **args)
        self.call(victim, 'note', text='死亡后个人记录')
        self.assertEqual(self.call(victim, 'my_info')['notes'][-1]['text'], '死亡后个人记录')

    def test_fifo_log_and_target_symlink_rejected_without_hanging(self):
        log = self.directory / 'events.jsonl'
        log.unlink()
        os.mkfifo(log, 0o600)
        self.client('p01').tool('my_info', error=True)
        log.unlink()
        (self.root / 'current').unlink()
        (self.root / 'alias').symlink_to('round-1')
        (self.root / 'current').symlink_to('alias')
        with self.assertRaises(RuleError):
            self.call('god', 'start_game', seed=7)

    def test_write_failures_never_acknowledged_or_silently_repaired(self):
        log = self.directory / 'events.jsonl'
        original = log.read_bytes()
        with mock.patch('engine.server.os.write', side_effect=OSError('disk full')):
            with self.assertRaises(RuleError):
                self.call('p01', 'note', text='not persisted')
        self.assertEqual(log.read_bytes(), original)
        real_write = os.write
        def partial(fd, payload):
            real_write(fd, payload[:17])
            raise OSError('interrupted write')
        with mock.patch('engine.server.os.write', side_effect=partial):
            with self.assertRaises(RuleError):
                self.call('p01', 'note', text='partial')
        corrupted = log.read_bytes()
        self.assertTrue(corrupted.startswith(original))
        with self.assertRaises(RuleError):
            self.call('p01', 'note', text='must not repair')
        self.assertEqual(log.read_bytes(), corrupted)

    def test_all_tool_schemas(self):
        # The MCP configs that start these servers are written by scripts/setup-team.sh.
        for actor in ('lead', 'god', *SEATS):
            with self.subTest(actor=actor):
                client = self.client(actor)
                self.assertIn('protocolVersion', client.rpc('initialize', {'protocolVersion': '2025-06-18'}))
                definitions = client.rpc('tools/list')['tools']
                self.assertEqual(len(definitions), {'lead': 1, 'god': 6}.get(actor, 15))
                for tool in definitions:
                    schema = tool['inputSchema']
                    self.assertFalse(schema['additionalProperties'])
                    self.assertTrue(set(schema['properties']).isdisjoint({'seat', 'role', 'player', 'identity', 'token', 'WW_SEAT'}))
                    self.assertEqual(set(schema['required']), set(schema['properties']))

    def test_uninitialized_step_and_strict_input_types(self):
        (self.root / 'round-2').mkdir(mode=0o700)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to('round-2')
        god = self.client('god')
        step = god.tool('next_step')
        self.assertEqual(step['phase'], 'uninitialized')
        self.assertEqual(step['calls'][0]['actions'], ['start_game'])
        god.tool('start_game', {'seed': True}, error=True)
        god.tool('start_game', {'seed': 7, 'role': 'wolf'}, error=True)
        self.client('p01').tool('start_game', {'seed': 7}, error=True)
        self.assertFalse((self.root / 'round-2' / 'events.jsonl').exists())
        god.tool('start_game', {'seed': 7})
        self.assertEqual(god.tool('next_step')['phase'], 'night_guard')

    def test_hunter_shot_last_wolf_ends_before_pending_badge_and_words(self):
        wolves = self.players('wolf')
        hunter = self.role('hunter')
        for victim in wolves[:-1]:
            self.night()
            if self.game().phase == 'sheriff_join':
                self.join([hunter])
            self.day_vote()
            self.votes({p: victim for p in self.game().living()})
            self.call('god', 'advance')
            self.effects()
        self.night()
        self.day_vote()
        self.votes({p: hunter for p in self.game().living()})
        self.call('god', 'advance')
        self.assertEqual([e[0] for e in self.game().effects], ['shoot', 'badge', 'last_words'])
        self.call(hunter, 'shoot', target=wolves[-1])
        self.assertEqual(self.game().winner, 'good')
        self.assertEqual(self.game().phase, 'finished')
        self.assertEqual(self.game().effects, [])


    # Players report sign-ups privately; the host announces the outcome once.
    def step(self):
        return self.client('god').tool('next_step')

    def test_sign_ups_and_withdrawals_are_announced_by_the_host(self):
        candidates = list(SEATS[:3])
        self.night()
        first = self.step()['calls'][-1]
        self.assertIn('私聊', first['say'])
        self.join(candidates)
        calls = self.step()['calls']
        self.assertEqual(calls[0]['say'], '上警的有一号、二号、三号，其余玩家在警下。')
        self.assertEqual((calls[0]['actions'], calls[0]['mentions']), ([], []))
        self.assertEqual(calls[1]['actions'], ['speak'])
        self.call(self.game().speakers[0], 'note', text='笔记不算推进')
        self.assertEqual(self.step()['calls'][0]['say'], calls[0]['say'])
        self.speech()
        self.assertFalse(any('上警的有' in c['say'] for c in self.step()['calls']))
        self.call(candidates[0], 'withdraw', withdraw=True)
        self.call(candidates[1], 'withdraw', withdraw=False)
        self.call(candidates[2], 'withdraw', withdraw=False)
        self.assertEqual(self.step()['calls'][0]['say'], '一号退水，二号、三号留在警上。')

    def test_a_single_candidate_is_announced_as_sheriff(self):
        self.night()
        self.join([SEATS[4]])
        self.assertEqual(self.step()['calls'][0]['say'], '只有五号上警，五号当选警长。')
        self.assertEqual(self.game().sheriff, SEATS[4])

    def test_nobody_withdrawing_is_said_and_everyone_withdrawing_ends_the_race(self):
        self.night()
        self.join(SEATS[:2])
        self.speech()
        for p in SEATS[:2]:
            self.call(p, 'withdraw', withdraw=True)
        self.assertEqual(self.step()['calls'][0]['say'], '全员退水，本局没有警长。')

    def test_nobody_withdrawing_is_announced(self):
        self.night()
        self.join(SEATS[:3])
        self.speech()
        for p in SEATS[:3]:
            self.call(p, 'withdraw', withdraw=False)
        self.assertEqual(self.step()['calls'][0]['say'], '没有人退水，一号、二号、三号留在警上。')

    def test_call_keys_are_stable_while_waiting_and_differ_between_lines(self):
        self.night()
        before = self.step()['calls'][-1]
        self.call(SEATS[0], 'sheriff_join', join=False)
        after = self.step()['calls'][-1]
        self.assertEqual(before['key'], after['key'])
        self.assertNotEqual(before['actors'], after['actors'])
        for p in SEATS[1:]:
            self.call(p, 'sheriff_join', join=False)
        keys = [c['key'] for c in self.step()['calls']]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn(before['key'], keys)

    def test_players_learn_the_host_member_id_from_the_seat_table(self):
        self.night()
        self.assertEqual(self.client(SEATS[0]).tool('my_info')['host'], 'god')
        (self.directory / 'seats.json').write_text(json.dumps({'seats': {'god': {'principal': 'g009-god'}}}))
        self.assertEqual(self.client(SEATS[0]).tool('my_info')['host'], 'g009-god')


    def test_the_host_records_the_wolves_group_once_and_gets_it_back(self):
        self.call(self.role('guard'), 'guard', target=None)
        self.assertEqual(self.game().phase, 'night_wolves')
        god = self.client('god')
        wolves = next(c for c in god.tool('next_step')['calls'] if c['target'] == 'wolves')
        self.assertIsNone(wolves['conversation_id'])
        self.assertEqual(god.tool('remember_conversation', {'target': 'wolves', 'conversation_id': 'wolf-group'}),
                         {'accepted': True})
        again = next(c for c in god.tool('next_step')['calls'] if c['target'] == 'wolves')
        self.assertEqual((again['conversation_id'], again['mapping_required']), ('wolf-group', False))
        # The same id again is fine; a second group is refused, so there is only ever one.
        god.tool('remember_conversation', {'target': 'wolves', 'conversation_id': 'wolf-group'})
        god.tool('remember_conversation', {'target': 'wolves', 'conversation_id': 'another-group'}, error=True)
        self.assertEqual(json.loads((self.directory / 'conversations.json').read_text()), {'wolves': 'wolf-group'})
        self.client(SEATS[0]).tool('remember_conversation', {'target': 'wolves', 'conversation_id': 'x'}, error=True)

    def models(self, **chosen):
        return {seat: chosen.get(seat, 'luna') for seat in ('god', *SEATS)}

    def test_new_game_opens_the_next_game_and_points_current_at_it(self):
        with self.assertRaisesRegex(RuleError, 'round-1 还没结束'):
            self.call('lead', 'new_game', models=self.models(), abandon_current=False)
        self.assertEqual(os.readlink(self.root / 'current'), 'round-1')
        opened = self.call('lead', 'new_game', models=self.models(p03='claude'), abandon_current=True)
        self.assertEqual((opened['game_id'], opened['previous']), ('g001', 'round-1'))
        game = self.root / 'g001'
        self.assertEqual(os.readlink(self.root / 'current'), 'g001')
        self.assertEqual(game.stat().st_mode & 0o777, 0o700)
        seats = json.loads((game / 'seats.json').read_text())['seats']
        self.assertEqual(seats, opened['seats'])
        self.assertEqual(seats['god'], {'model': 'luna', 'principal': 'g001-god'})
        self.assertEqual(seats['p03'], {'model': 'claude', 'principal': 'g001-p03'})
        self.assertEqual(sorted(seats), sorted(('god', *SEATS)))
        # The abandoned game is left as it was, and the new one starts empty and reads its seats.
        self.assertTrue((self.directory / 'events.jsonl').exists())
        self.assertFalse(self.game().started)
        self.call('god', 'start_game', seed=3)
        self.assertEqual(self.call('p01', 'my_info')['host'], 'g001-god')

    def test_new_game_takes_the_smallest_unused_number(self):
        for name in ('g001', 'g003', 'notes'):
            (self.root / name).mkdir()
        (self.root / 'archive' / 'run1-g002').mkdir(parents=True)
        self.assertEqual(self.call('lead', 'new_game', models=self.models(), abandon_current=True)['game_id'], 'g002')
        self.assertEqual(self.call('lead', 'new_game', models=self.models(), abandon_current=False)['game_id'], 'g004')

    def test_new_game_works_without_a_current_game(self):
        (self.root / 'current').unlink()
        opened = self.call('lead', 'new_game', models=self.models(), abandon_current=False)
        self.assertIsNone(opened['previous'])
        self.assertEqual(os.readlink(self.root / 'current'), 'g001')

    def test_only_the_lead_opens_games_and_it_does_nothing_else(self):
        with self.assertRaises(RuleError):
            self.call('god', 'new_game', models=self.models(), abandon_current=True)
        for action, args in (('my_info', {}), ('next_step', {}), ('note', {'text': 'x'})):
            with self.subTest(action=action), self.assertRaises(RuleError):
                self.call('lead', action, **args)
        models = self.models()
        del models['p12']
        for bad in (models, {**self.models(), 'p13': 'luna'}, self.models(p01=''), self.models(p01=3)):
            with self.subTest(models=bad), self.assertRaises(RuleError):
                self.call('lead', 'new_game', models=bad, abandon_current=True)
        self.assertEqual(os.readlink(self.root / 'current'), 'round-1')
        lead = self.client('lead')
        self.assertEqual([t['name'] for t in lead.rpc('tools/list')['tools']], ['new_game'])
        self.assertIn('还没结束', lead.tool('new_game', {'models': self.models(), 'abandon_current': False}, error=True))

    def test_start_game_without_a_seed_uses_the_time_and_logs_it(self):
        (self.root / 'current').unlink()
        self.call('lead', 'new_game', models=self.models(), abandon_current=False)
        started = self.call('god', 'start_game', seed=None)
        seed = started['seed']
        self.assertRegex(str(seed), r'^20\d{12}$')
        log = [json.loads(line) for line in (self.root / 'g001' / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(log[0]['args'], {'seed': seed})
        self.assertEqual(self.game().seed, seed)
        with self.assertRaises(RuleError):
            Game().apply('god', 'start_game', {'seed': None})


if __name__ == '__main__':
    unittest.main()
