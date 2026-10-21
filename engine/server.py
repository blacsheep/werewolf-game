#!/usr/bin/env python3
"""12-seat werewolf MCP server. Python 3.10+, POSIX; no third-party packages."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import stat
import sys

SEATS = tuple(f'p{i:02}' for i in range(1, 13))
ROLES = ['wolf'] * 4 + ['villager'] * 4 + ['seer', 'witch', 'hunter', 'guard']
SPEECH_PHASES = {'sheriff_speech', 'sheriff_pk_speech', 'day_speech', 'exile_pk_speech'}
ROLE_ADVICE = {
    'wolf': '与队友统一刀口与发言口径；仅轮到自己发言时可自爆。',
    'villager': '通过发言和完整票型盘狼，明确立场与放逐目标。',
    'seer': '记录查验结果，发言时可自愿交代警徽流。',
    'witch': '解药毒药各一瓶，每夜只能用一瓶，全程不能自救。',
    'hunter': '被刀或放逐可开枪；被毒不能开枪。',
    'guard': '可以空守或守自己，不能连续两晚守同一人；注意同守同救。',
}


class RuleError(Exception):
    pass


def require(condition, message='当前身份或阶段不允许此操作'):
    if not condition:
        raise RuleError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def rotated(items, seed, label):
    if not items:
        return []
    start = random.Random(f'{seed}:{label}').randrange(len(items))
    return items[start:] + items[:start]


class Game:
    """Disposable projection: recreated exclusively from validated command events."""

    def __init__(self):
        self.started = False

    def living(self):
        return [p for p in SEATS if p in self.alive]

    def role_seat(self, role):
        return next(p for p in SEATS if self.roles[p] == role)

    def public(self, kind, **fields):
        self.history.append({'kind': kind, 'day': self.day, **fields})

    def check_win(self):
        roles = [self.roles[p] for p in self.alive]
        if 'wolf' not in roles:
            self.winner = 'good'
        elif 'villager' not in roles or not any(r not in ('wolf', 'villager') for r in roles):
            self.winner = 'wolves'
        if self.winner:
            self.phase = 'finished'
            self.public('game_over', winner=self.winner)
            return True
        return False

    def new_night(self):
        self.day += 1
        self.night = {'guard': None, 'kill': None, 'save': False, 'poison': None}
        self.night_acted = False
        self.phase = 'night_guard'
        self.pending_deaths = []
        self.public('night_started')
        self.skip_absent()

    def skip_absent(self):
        phases = ['night_guard', 'night_wolves', 'night_witch', 'night_seer']
        while self.phase in phases:
            role = {'night_guard': 'guard', 'night_wolves': 'wolf',
                    'night_witch': 'witch', 'night_seer': 'seer'}[self.phase]
            if any(self.roles[p] == role for p in self.alive):
                return
            self.phase = (phases + ['night_done'])[phases.index(self.phase) + 1]
        if self.phase == 'night_done':
            self.finish_night()

    def night_next(self):
        self.night_acted = True
        phases = ['night_guard', 'night_wolves', 'night_witch', 'night_seer', 'night_done']
        self.phase = phases[phases.index(self.phase) + 1]
        self.skip_absent()

    def finish_night(self):
        kill = self.night['kill']
        guarded = kill is not None and self.night['guard'] == kill
        deaths = {}
        if kill is not None and guarded == self.night['save']:
            deaths[kill] = 'knife'
        if self.night['poison']:
            deaths[self.night['poison']] = 'poison'
        self.pending_deaths = [(p, deaths[p]) for p in SEATS if p in deaths]
        self.phase = 'sheriff_join' if self.day == 1 else 'announce'
        if self.day == 1:
            self.joins = {}

    def begin_speech(self, phase, order):
        self.phase = phase
        self.speakers = list(order)

    def begin_vote(self, kind, candidates, voters, round_number=1):
        self.vote_state = {'id': f'{self.day}:{kind}:{round_number}', 'kind': kind,
                           'round': round_number, 'candidates': list(candidates),
                           'voters': list(voters), 'ballots': {}}
        self.phase = 'vote' if voters else 'vote_result'

    def vote_result(self):
        vote = self.vote_state
        require(self.phase == 'vote_result', '全部有效票完成后才可查看票型')
        totals = {p: 0 for p in vote['candidates']}
        weights = {}
        for p, target in vote['ballots'].items():
            weight = 3 if vote['kind'] == 'exile' and p == self.sheriff else 2
            weights[p] = weight / 2
            if target:
                totals[target] += weight
        return {**vote, 'weights': weights, 'totals': {p: n / 2 for p, n in totals.items()}}

    def complete_vote(self):
        result = self.vote_result()
        self.public('vote_result', vote_kind=result['kind'],
                    **{k: v for k, v in result.items() if k != 'kind'})
        high = max(result['totals'].values(), default=0)
        tied = [p for p, n in result['totals'].items() if n == high] if high else []
        if len(tied) > 1 and result['round'] == 1:
            self.pk = tied
            self.begin_speech(result['kind'] + '_pk_speech', tied)
        elif result['kind'] == 'sheriff':
            self.sheriff = tied[0] if len(tied) == 1 else None
            self.public('sheriff_elected', sheriff=self.sheriff)
            self.phase = 'announce'
        elif len(tied) == 1:
            self.public('exiled', player=tied[0])
            self.death_batch([(tied[0], 'exile')], 'night')
        else:
            self.public('no_exile')
            self.new_night()

    def death_batch(self, deaths, resume, last_words=True):
        # Night deaths are simultaneous; poison overrides knife on the same seat.
        self.alive.difference_update(p for p, _ in deaths)
        self.deaths.update(dict(deaths))
        self.resume = resume
        if self.check_win():
            self.effects = []
            return
        effects = []
        for p, cause in deaths:
            if self.roles[p] == 'hunter' and cause in ('knife', 'exile'):
                effects.append(('shoot', p))
            if p == self.sheriff:
                effects.append(('badge', p))
            if last_words and (cause == 'exile' or (self.day == 1 and cause in ('knife', 'poison'))):
                effects.append(('last_words', p))
        self.effects.extend(effects)
        self.next_effect()

    def next_effect(self):
        if self.effects:
            self.phase = self.effects[0][0]
        elif self.resume == 'night':
            self.new_night()
        elif self.sheriff:
            self.phase = 'choose_order'
        else:
            alive = self.living()
            if self.last_night_dead:
                start = SEATS.index(self.last_night_dead[0]) + 1
                order = list(SEATS[start:]) + list(SEATS[:start])
                order = [p for p in order if p in self.alive]
            else:
                order = rotated(alive, self.seed, f'day:{self.day}')
            self.begin_speech('day_speech', order)

    def announce(self, resume='day', last_words=True):
        deaths = self.pending_deaths
        self.pending_deaths = []
        self.last_night_dead = [p for p, _ in deaths]
        self.public('night_deaths', players=self.last_night_dead)
        self.death_batch(deaths, resume, last_words)

    def target_alive(self, target, nullable=False):
        require((nullable and target is None) or target in self.alive, '目标必须是存活座位')

    def apply(self, actor, action, args):
        if action == 'start_game':
            require(actor == 'god' and not self.started, '只有上帝可初始化空对局')
            self.started = True
            self.seed = args['seed']
            roles = list(ROLES)
            random.Random(self.seed).shuffle(roles)
            self.roles = dict(zip(SEATS, roles))
            self.alive = set(SEATS)
            self.deaths = {}
            self.winner = None
            self.sheriff = None
            self.badge_lost = False
            self.guard_last = None
            self.antidote = self.poison = True
            self.inspections = []
            self.notes = {p: [] for p in SEATS}
            self.history = []
            self.effects = []
            self.last_night_dead = []
            self.notice = None
            self.day = 0
            self.new_night()
            return
        require(self.started, '对局尚未初始化')
        if action not in ('note', 'vote'):
            # A host notice stands until the next phase gets going. Single ballots leave
            # it alone, so the host cannot tell how far a secret vote has got.
            self.notice = None
        if action == 'note':
            require(actor in SEATS)
            self.notes[actor].append({'day': self.day, 'text': args['text']})
            return
        require(not self.winner, '对局已结束')
        if action == 'advance':
            require(actor == 'god')
            if self.phase == 'announce':
                self.announce()
            elif self.phase == 'vote_result':
                self.complete_vote()
            else:
                raise RuleError('当前阶段须由玩家提交动作，上帝不能代操作')
            return
        require(actor in SEATS and actor != 'god')
        require(action in self.available(actor))
        target = args.get('target')
        if action == 'guard':
            self.target_alive(target, True)
            require(target is None or target != self.guard_last, '不能连续两晚守同一人')
            self.guard_last = target
            self.night['guard'] = target
            self.night_next()
        elif action == 'wolf_kill':
            self.target_alive(target, True)
            self.night['kill'] = target
            self.night_next()
        elif action == 'witch':
            mode = args['mode']
            require(mode == 'poison' or target is None, '仅毒药操作接受目标')
            if mode == 'save':
                require(self.antidote and self.night['kill'] is not None, '当前不能使用解药')
                require(self.night['kill'] != actor, '女巫全程不能自救')
                self.antidote = False
                self.night['save'] = True
            elif mode == 'poison':
                self.target_alive(target)
                require(self.poison, '毒药已使用')
                self.poison = False
                self.night['poison'] = target
            self.night_next()
        elif action == 'inspect':
            self.target_alive(target)
            self.inspections.append({'day': self.day, 'target': target,
                                     'alignment': 'wolf' if self.roles[target] == 'wolf' else 'good'})
            self.night_next()
        elif action == 'sheriff_join':
            self.joins[actor] = args['join']
            self.public('sheriff_registration', player=actor, join=args['join'])
            if len(self.joins) == len(self.alive):
                self.candidates = [p for p in SEATS if self.joins.get(p)]
                self.electorate = [p for p in self.living() if not self.joins[p]]
                self.withdrawals = {}
                if len(self.candidates) <= 1 or not self.electorate:
                    self.sheriff = self.candidates[0] if len(self.candidates) == 1 else None
                    self.public('sheriff_elected', sheriff=self.sheriff)
                    self.phase = 'announce'
                    self.notice = (f'只有{seat_name(self.sheriff)}上警，{seat_name(self.sheriff)}当选警长。' if self.sheriff
                                   else '全员上警，没有人能投票，本局没有警长。' if self.candidates
                                   else '没有人上警，本局没有警长。')
                else:
                    self.begin_speech('sheriff_speech', rotated(self.candidates, self.seed, 'sheriff'))
                    self.notice = f'上警的有{seat_list(self.candidates)}，其余玩家在警下。'
        elif action == 'withdraw':
            self.withdrawals[actor] = args['withdraw']
            self.public('withdrawal', player=actor, withdraw=args['withdraw'])
            if len(self.withdrawals) == len(self.candidates):
                remaining = [p for p in self.candidates if not self.withdrawals[p]]
                quitters = [p for p in self.candidates if self.withdrawals[p]]
                left = f'{seat_list(quitters)}退水，' if quitters else '没有人退水，'
                if len(remaining) <= 1:
                    self.sheriff = remaining[0] if remaining else None
                    self.public('sheriff_elected', sheriff=self.sheriff)
                    self.phase = 'announce'
                    self.notice = (f'{left}{seat_name(self.sheriff)}当选警长。' if self.sheriff
                                   else '全员退水，本局没有警长。')
                else:
                    self.begin_vote('sheriff', remaining, self.electorate)
                    self.notice = f'{left}{seat_list(remaining)}留在警上。'
        elif action == 'speak':
            phase = self.phase
            self.public('speech', player=actor, phase=phase, text=args['text'])
            self.speakers.pop(0)
            if not self.speakers:
                if phase == 'sheriff_speech':
                    self.phase = 'withdraw'
                elif phase == 'day_speech':
                    self.begin_vote('exile', self.living(), self.living())
                elif phase == 'sheriff_pk_speech':
                    self.begin_vote('sheriff', self.pk, self.electorate, 2)
                else:
                    self.begin_vote('exile', self.pk, [p for p in self.living() if p not in self.pk], 2)
        elif action == 'choose_order':
            pivot = SEATS.index(actor)
            delta = 1 if args['direction'] == 'left' else -1
            order = [SEATS[(pivot + delta * i) % 12] for i in range(1, 13)]
            self.public('speech_direction', player=actor, direction=args['direction'])
            self.begin_speech('day_speech', [p for p in order if p in self.alive])
        elif action == 'vote':
            require(target is None or target in self.vote_state['candidates'], '不能投给非候选座位')
            self.vote_state['ballots'][actor] = target
            if len(self.vote_state['ballots']) == len(self.vote_state['voters']):
                self.phase = 'vote_result'
                self.notice = None
        elif action in ('shoot', 'badge', 'last_words'):
            if action != 'last_words':
                self.target_alive(target, True)
            self.effects.pop(0)
            if action == 'shoot':
                self.public('hunter_shot', player=actor, target=target)
                if target:
                    self.death_batch([(target, 'shot')], self.resume, False)
                else:
                    self.next_effect()
            elif action == 'badge':
                self.sheriff = target
                self.badge_lost = target is None
                self.public('badge_transferred' if target else 'badge_destroyed', player=actor, target=target)
                self.next_effect()
            else:
                self.public('last_words', player=actor, text=args['text'])
                self.next_effect()
        elif action == 'explode':
            campaign = self.phase.startswith('sheriff_')
            pending = list(self.pending_deaths) if campaign else []
            self.public('explosion', player=actor)
            if campaign:
                self.sheriff = None
                self.badge_lost = True
                self.public('badge_lost')
                self.pending_deaths = []
                self.last_night_dead = [p for p, _ in pending]
                self.public('night_deaths', players=self.last_night_dead)
            # Settle the previous night too, preserving its first-night death actions.
            # Explosion and pending deaths are checked together before those actions.
            deaths = [(actor, 'explode')] + [(p, cause) for p, cause in pending if p != actor]
            self.death_batch(deaths, 'night', last_words=campaign)

    def available(self, actor):
        if not self.started:
            return []
        if self.winner:
            return ['note'] if actor in SEATS else ['export_replay']
        if actor == 'god':
            return ['advance'] if self.phase in ('announce', 'vote_result') else []
        actions = ['note']
        if self.effects and self.effects[0][1] == actor:
            actions.append(self.effects[0][0])
        if actor not in self.alive:
            return actions
        role = self.roles[actor]
        action = {'night_guard': ('guard', 'guard'), 'night_wolves': ('wolf', 'wolf_kill'),
                  'night_witch': ('witch', 'witch'), 'night_seer': ('seer', 'inspect')}.get(self.phase)
        if action and role == action[0]:
            actions.append(action[1])
        if self.phase == 'sheriff_join' and actor not in self.joins:
            actions.append('sheriff_join')
        if self.phase == 'withdraw' and actor in self.candidates and actor not in self.withdrawals:
            actions.append('withdraw')
        if self.phase in SPEECH_PHASES and self.speakers[0] == actor:
            actions.append('speak')
            if role == 'wolf':
                actions.append('explode')
        if self.phase == 'choose_order' and actor == self.sheriff:
            actions.append('choose_order')
        if self.phase == 'vote' and actor in self.vote_state['voters'] and actor not in self.vote_state['ballots']:
            actions.append('vote')
        return actions

    def info(self, actor, principals=None):
        require(self.started, '对局尚未初始化')
        phase = 'night' if self.phase.startswith('night_') and actor != 'god' else self.phase
        result = {'day': self.day, 'phase': phase, 'alive': self.living(), 'sheriff': self.sheriff,
                  'winner': self.winner, 'history': self.history}
        if actor == 'god':
            result['roles'] = self.roles
        else:
            result.update({'self': actor, 'role': self.roles[actor], 'notes': self.notes[actor],
                           'advice': ROLE_ADVICE[self.roles[actor]],
                           # Member id of the host, for the private chat where players report actions.
                           'host': (principals or {}).get('god', 'god')})
            # During voting even own status is stable, preventing progress disclosure.
            result['actions'] = self.available(actor)
            if self.phase == 'vote' and actor in self.vote_state['voters'] and 'vote' not in result['actions']:
                result['actions'].append('vote')
            role = self.roles[actor]
            if role == 'wolf':
                result['teammates'] = [p for p in SEATS if self.roles[p] == 'wolf']
            elif role == 'seer':
                result['inspections'] = self.inspections
            elif role == 'witch':
                result['potions'] = {'antidote': self.antidote, 'poison': self.poison}
                if self.phase == 'night_witch' and actor in self.alive and self.antidote:
                    result['knife_target'] = self.night['kill']
            elif role == 'guard':
                result['last_guard'] = self.guard_last
        if self.phase in ('vote', 'vote_result'):
            result['voting'] = {k: v for k, v in self.vote_state.items() if k != 'ballots'}
        if self.phase in SPEECH_PHASES:
            result['speaker'] = self.speakers[0]
        return result

    def next_step(self, conversations, principals=None):
        """What the host does next. `say` is the host's line, ready to send as written;
        `instruction` is a note for the host only and never goes into a conversation.
        `mentions` holds the members to mention: seat ids mapped to agt members via seats.json."""
        principals = principals or {}
        if not self.started:
            return {'day': 0, 'phase': 'uninitialized', 'winner': None,
                    'calls': [{'actors': ['god'], 'target': 'public',
                               'conversation_id': conversations.get('public'),
                               'mapping_required': 'public' not in conversations,
                               'mentions': [], 'actions': ['start_game'], 'say': '本局游戏开始，请各位确认自己的身份。',
                               'instruction': '先调用 start_game(seed) 初始化对局；种子不公开。'}]}
        phase = self.phase
        calls = []

        def call(actors, target, actions, say, instruction):
            # The same line on the same day and phase always has the same key, so a host
            # that sends it again (from another run) is deduplicated, not repeated.
            key = hashlib.sha256(f'{self.day}|{phase}|{target}|{say}'.encode()).hexdigest()[:16]
            calls.append({'actors': actors, 'target': target,
                          'conversation_id': conversations.get(target),
                          'mapping_required': target not in conversations,
                          'mentions': [principals.get(p, p) for p in actors if p != 'god'],
                          'actions': actions, 'say': say, 'instruction': instruction, 'key': key})

        if self.notice:
            call([], 'public', [], self.notice, '先在公共群公布这句，再做下一条。')

        if phase.startswith('night_'):
            if not self.night_acted:
                call([], 'public', [], '天黑请闭眼。', '先在公共群宣布入夜，再叫醒第一个角色。')
            role = {'night_guard': 'guard', 'night_wolves': 'wolf', 'night_witch': 'witch',
                    'night_seer': 'seer'}[phase]
            actors = [p for p in self.living() if self.roles[p] == role]
            action = {'guard': 'guard', 'wolf': 'wolf_kill', 'witch': 'witch', 'seer': 'inspect'}[role]
            call(actors, 'wolves' if role == 'wolf' else f'private:{actors[0]}', [action],
                 self.night_line(role),
                 '在该角色的私密会话发出 say；玩家自己完成行动。狼人协商一致后由其中一人行动。')
        elif phase == 'sheriff_join':
            call([p for p in self.living() if p not in self.joins], 'public', ['sheriff_join'],
                 '天亮了。现在开始竞选警长，请各位私聊告诉我是否上警。',
                 '一次点名全部待表态的玩家。玩家在和你的私聊里表态；全部表态后再调用 next_step，会拿到要公布的上警名单。')
        elif phase == 'withdraw':
            call([p for p in self.candidates if p not in self.withdrawals], 'public', ['withdraw'],
                 '警上发言结束，请各位候选人私聊告诉我是否退水。',
                 '一次点名全部候选人。候选人在和你的私聊里表态；退水者不能投票，也不能被投。')
        elif phase in SPEECH_PHASES:
            actor = self.speakers[0]
            call([actor], 'public', [a for a in self.available(actor) if a != 'note'],
                 self.speech_line(phase, actor), '只点名这一位发言者。')
        elif phase == 'choose_order':
            call([self.sheriff], 'public', ['choose_order'],
                 f'请警长{seat_name(self.sheriff)}选择从警左还是警右开始发言。', '')
        elif phase == 'vote':
            voters = list(self.vote_state['voters'])
            call(voters, 'public', ['vote'], self.vote_line(),
                 '一条消息点名全部有投票权的座位；玩家私下投票，投完在和你的私聊里说“已投票”。'
                 '已投的人不用再投；不要按投票进度缩减点名名单。')
            calls[-1].update(voters=voters)
        elif phase in ('shoot', 'badge', 'last_words'):
            actor = self.effects[0][1]
            line = {'shoot': f'{seat_name(actor)}，请决定是否发动技能。',
                    'badge': f'警长{seat_name(actor)}，请移交警徽，或者撕毁警徽。',
                    'last_words': f'请{seat_name(actor)}发表遗言。'}[phase]
            call([actor], 'public', [phase], line, '')
        elif phase == 'announce':
            dead = [p for p, _ in self.pending_deaths]
            line = f'现在公布昨夜信息：昨夜{seat_list(dead)}出局。' if dead else '现在公布昨夜信息：昨夜是平安夜。'
            call(['god'], 'public', ['advance'], line, '先调用 advance 结算，再发出 say；不公布死因。')
        elif phase == 'vote_result':
            call(['god'], 'public', ['advance'], self.vote_result_line(), '先调用 advance 结算，再发出 say。')
        elif phase == 'finished':
            winner = '好人' if self.winner == 'good' else '狼人'
            roles = '；'.join(f'{seat_name(p)}{ROLE_NAMES[self.roles[p]]}' for p in SEATS)
            call(['god'], 'public', ['export_replay'], f'游戏结束，{winner}阵营获胜。全员身份：{roles}。', '')
        result = {'day': self.day, 'phase': phase, 'calls': calls, 'winner': self.winner}
        if phase == 'vote_result':
            result['vote_result'] = self.vote_result()
        return result

    def night_line(self, role):
        if role == 'guard':
            return '守卫请睁眼，今晚你要守护谁？'
        if role == 'wolf':
            return '狼人请睁眼，请商量今晚的目标，商量好后由一人动手。'
        if role == 'seer':
            return '预言家请睁眼，今晚你要查验谁？'
        kill = self.night['kill']
        if self.antidote and kill:
            return f'女巫请睁眼。今晚{seat_name(kill)}倒下了，你要使用解药吗？要使用毒药吗？'
        if self.antidote:
            return '女巫请睁眼。今晚是平安夜，你要使用毒药吗？'
        return '女巫请睁眼。你要使用毒药吗？'

    def speech_line(self, phase, actor):
        name = seat_name(actor)
        if phase == 'sheriff_speech':
            return f'请{name}进行警上发言。'
        if phase == 'sheriff_pk_speech':
            return f'请{name}进行警长 PK 发言。'
        if phase == 'exile_pk_speech':
            return f'请{name}进行 PK 发言。'
        if actor == self.sheriff and len(self.speakers) == 1:
            return f'请警长{name}发言并归票。'
        return f'请{name}发言。'

    def vote_line(self):
        vote = self.vote_state
        if vote['kind'] == 'sheriff':
            if vote['round'] == 1:
                return '请警下玩家投票选出警长，可以弃票。'
            return f'请警下玩家在{seat_list(vote["candidates"])}之间投票。'
        if vote['round'] == 1:
            return '发言结束，请所有存活玩家投票，可以弃票。'
        return f'请{seat_list(vote["voters"])}在{seat_list(vote["candidates"])}之间投票。'

    def vote_result_line(self):
        result = self.vote_result()
        by_target = {}
        for voter, target in result['ballots'].items():
            by_target.setdefault(target, []).append(voter)
        parts = [f'{seat_list(v)}投给{seat_name(t)}' for t, v in by_target.items() if t]
        if None in by_target:
            parts.append(f'{seat_list(by_target[None])}弃票')
        totals = {p: n for p, n in result['totals'].items() if n}
        high = max(totals.values(), default=0)
        tied = [p for p, n in totals.items() if n == high]
        sheriff = result['kind'] == 'sheriff'
        if len(tied) == 1:
            outcome = f'{seat_name(tied[0])}当选警长。' if sheriff else f'{seat_name(tied[0])}被放逐。'
        elif tied and result['round'] == 1:
            outcome = f'{seat_list(tied)}平票，进入 PK。'
        else:
            outcome = '本局没有警长。' if sheriff else '今天无人出局。'
        return f'投票结果：{"；".join(parts) or "所有人弃票"}。{outcome}'


CN_NUMBERS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二']
ROLE_NAMES = {'wolf': '狼人', 'villager': '村民', 'seer': '预言家', 'witch': '女巫',
              'hunter': '猎人', 'guard': '守卫'}


def seat_name(seat):
    return CN_NUMBERS[SEATS.index(seat)] + '号'


def seat_list(seats):
    return '、'.join(seat_name(p) for p in seats)


TARGET = {'type': ['string', 'null'], 'enum': [*SEATS, None]}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 20000}
TOOLS = {
    'my_info': ('both', '读取公开历史和自己的私有信息；上帝可见角色表。', {}),
    'start_game': ('god', '以记录的整数种子初始化当前空对局。', {'seed': {'type': 'integer'}}),
    'next_step': ('god', '当前阶段的点名、会话映射、开放操作；票齐后提供票型。', {}),
    'advance': ('god', '公布死讯或完整票型并自动执行结算；不能代玩家行动。', {}),
    'export_replay': ('god', '仅完局后导出完整可重放 events.jsonl。', {}),
    'remember_conversation': ('god', '记下本局某个逻辑会话（wolves 或 private:pXX）对应的群或私聊 ID；之后 next_step 直接返回它。',
                              {'target': {'type': 'string', 'enum': ['wolves', *(f'private:{p}' for p in SEATS)]},
                               'conversation_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}),
    'guard': ('player', '守护存活目标；null 空守。', {'target': TARGET}),
    'wolf_kill': ('player', '狼队协商后一名存活狼人提交刀口；null 空刀。', {'target': TARGET}),
    'witch': ('player', '女巫选择 pass/save/poison；仅 poison 填目标。',
              {'mode': {'type': 'string', 'enum': ['pass', 'save', 'poison']}, 'target': TARGET}),
    'inspect': ('player', '预言家查验存活目标；结果在 my_info 中。', {'target': {'type': 'string', 'enum': list(SEATS)}}),
    'sheriff_join': ('player', '首日确认上警或不上警。', {'join': {'type': 'boolean'}}),
    'withdraw': ('player', '警上发言结束后确认退水或继续竞选。', {'withdraw': {'type': 'boolean'}}),
    'speak': ('player', '轮到自己时提交发言，警长末位发言包含归票。', {'text': TEXT}),
    'choose_order': ('player', '警长选择警左（座号递增）或警右（递减）。', {'direction': {'type': 'string', 'enum': ['left', 'right']}}),
    'vote': ('player', '一次性秘密投票；null 弃票。', {'target': TARGET}),
    'last_words': ('player', '提交引擎安排的遗言。', {'text': TEXT}),
    'shoot': ('player', '符合死亡原因的猎人开枪；null 不开枪。', {'target': TARGET}),
    'badge': ('player', '出局警长移交警徽；null 撕毁。', {'target': TARGET}),
    'explode': ('player', '仅狼人轮到自己白天发言时自爆。', {}),
    'note': ('player', '追加个人私密笔记，死亡及完局后仍可记录。', {'text': TEXT}),
}


def validate_args(name, args):
    require(name in TOOLS, '未知工具')
    props = TOOLS[name][2]
    require(isinstance(args, dict) and set(args) == set(props), '参数必须严格匹配工具定义；不接受身份覆盖参数')
    types = {'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
    for key, schema in props.items():
        value = args[key]
        allowed = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        require(any(type(value) is types[t] for t in allowed), '参数类型错误')
        require('enum' not in schema or value in schema['enum'], '参数值不在允许范围')
        if isinstance(value, str):
            require(schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 20000), '文本长度不合法')


def tool_list(actor):
    return [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': props,
                             'required': list(props), 'additionalProperties': False}}
            for name, (audience, description, props) in TOOLS.items()
            if audience == 'both' or audience == ('god' if actor == 'god' else 'player')]


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()

    @contextlib.contextmanager
    def locked(self):
        """One lock for all seats and current-link switches. Lock file is not state."""
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                lock_fd = os.open('.engine.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                try:
                    require(stat.S_ISREG(os.fstat(lock_fd).st_mode) and os.fstat(lock_fd).st_nlink == 1, '锁文件不安全')
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    game_id = os.readlink('current', dir_fd=root_fd)
                    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', game_id) is not None and game_id != 'current',
                            'current 必须是指向 games 直接子目录的相对符号链接')
                    directory_fd = os.open(game_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                    try:
                        yield root_fd, directory_fd, game_id
                    finally:
                        os.close(directory_fd)
                finally:
                    os.close(lock_fd)
            finally:
                os.close(root_fd)
        except OSError as exc:
            raise RuleError('对局存储不可用：检查 current 符号链接、目录、权限与日志') from exc

    def load(self, directory_fd):
        game = Game()
        try:
            fd = os.open('events.jsonl', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return game, [], b''
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_mode & 0o077 == 0, '审计日志必须是独立的 0600 普通文件')
            raw = stream.read()
        require(not raw or raw.endswith(b'\n'), '日志尾部不完整，拒绝重放或追加')
        events = []
        previous = '0' * 64
        for line in raw.splitlines():
            try:
                event = json.loads(line)
                require(set(event) == {'version', 'seq', 'at', 'actor', 'action', 'args', 'prev', 'hash'})
                unsigned = {k: v for k, v in event.items() if k != 'hash'}
                require(event['version'] == 1 and event['seq'] == len(events) + 1 and event['prev'] == previous)
                require(event['hash'] == hashlib.sha256(canonical(unsigned).encode()).hexdigest())
                validate_args(event['action'], event['args'])
                require(event['actor'] in ('god', *SEATS))
                require(event['action'] not in ('my_info', 'next_step', 'export_replay'))
                game.apply(event['actor'], event['action'], event['args'])
                previous = event['hash']
                events.append(event)
            except (ValueError, TypeError, KeyError, RuleError, AttributeError) as exc:
                raise RuleError(f'审计日志第 {len(events) + 1} 条损坏，拒绝重放或追加') from exc
        return game, events, raw

    def conversations(self, directory_fd):
        """Conversations the host recorded for this game (conversations.json), if any."""
        try:
            fd = os.open('conversations.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'rb') as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'conversations.json 必须是普通文件')
            try:
                recorded = json.loads(stream.read())
            except ValueError as exc:
                raise RuleError('conversations.json 不是有效的 JSON') from exc
        require(isinstance(recorded, dict) and all(isinstance(v, str) and v for v in recorded.values()),
                'conversations.json 必须是 {会话: ID} 对象')
        return recorded

    def principals(self, directory_fd):
        """Seat (and god) → agt member id from the game's seats.json (`seats.<seat>.principal`)."""
        try:
            fd = os.open('seats.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'rb') as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'seats.json 必须是普通文件')
            try:
                seats = json.loads(stream.read()).get('seats', {})
            except (ValueError, AttributeError) as exc:
                raise RuleError('seats.json 不是有效的 JSON 对象') from exc
        require(isinstance(seats, dict), 'seats.json 的 seats 必须是对象')
        result = {}
        for seat in ('god', *SEATS):
            principal = (seats.get(seat) or {}).get('principal') if isinstance(seats.get(seat), dict) else None
            if principal is not None:
                require(isinstance(principal, str) and principal, f'seats.json 中 {seat} 的 principal 必须是非空字符串')
                result[seat] = principal
        return result

    def call(self, actor, name, args, conversations=None):
        validate_args(name, args)
        audience = TOOLS[name][0]
        require(actor in ('god', *SEATS), '身份无效')
        require(audience == 'both' or audience == ('god' if actor == 'god' else 'player'))
        with self.locked() as (root_fd, directory_fd, game_id):
            game, events, raw = self.load(directory_fd)
            if name == 'my_info':
                return game.info(actor, self.principals(directory_fd))
            if name == 'remember_conversation':
                remembered = self.conversations(directory_fd)
                require(remembered.get(args['target']) in (None, args['conversation_id']),
                        f"本局的 {args['target']} 已记为另一个会话，继续用它，不要再建新的")
                remembered[args['target']] = args['conversation_id']
                payload = (json.dumps(remembered, ensure_ascii=False, indent=2) + '\n').encode()
                fd = os.open('conversations.json.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644,
                             dir_fd=directory_fd)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                os.replace('conversations.json.tmp', 'conversations.json', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                return {'accepted': True}
            if name == 'next_step':
                # Conversations the host recorded in this game come after the deployment's.
                mapping = {**(conversations or {}), **self.conversations(directory_fd)}
                return game.next_step(mapping, self.principals(directory_fd))
            if name == 'export_replay':
                require(game.started and game.winner, '仅完局后允许完整导出')
                return {'format': 'werewolf-events-v1', 'game_id': game_id,
                        'winner': game.winner, 'roles': game.roles, 'events': events, 'jsonl': raw.decode()}
            game.apply(actor, name, args)
            event = {'version': 1, 'seq': len(events) + 1,
                     'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'actor': actor, 'action': name, 'args': args,
                     'prev': events[-1]['hash'] if events else '0' * 64}
            event['hash'] = hashlib.sha256(canonical(event).encode()).hexdigest()
            require(os.readlink('current', dir_fd=root_fd) == game_id, 'current 已切换，拒绝写入')
            current_stat = os.stat('current', dir_fd=root_fd)
            opened_stat = os.fstat(directory_fd)
            require((current_stat.st_dev, current_stat.st_ino) == (opened_stat.st_dev, opened_stat.st_ino), 'current 目标已变化')
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
            if not events and not raw:
                # Existing empty log is allowed, but never truncate any file.
                flags |= os.O_CREAT
            fd = os.open('events.jsonl', flags, 0o600, dir_fd=directory_fd)
            try:
                st = os.fstat(fd)
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_mode & 0o077 == 0 and st.st_size == len(raw),
                        '日志权限、类型或长度发生异常变化')
                payload = (canonical(event) + '\n').encode()
                while payload:
                    written = os.write(fd, payload)
                    require(written > 0, '事件写入失败')
                    payload = payload[written:]
                os.fsync(fd)
                os.fsync(directory_fd)
            finally:
                os.close(fd)
            # Never return state/sequence or partial vote information on mutation.
            return {'accepted': True}


class Server:
    def __init__(self):
        self.actor = os.environ.get('WW_SEAT')
        require(self.actor in ('god', *SEATS), 'WW_SEAT 必须为 god 或 p01…p12，且不可缺失')
        self.store = Store(os.environ.get('WW_GAMES_DIR', Path(__file__).resolve().parent.parent / 'games'))
        self.conversations = json.loads(os.environ.get('WW_CONVERSATIONS', '{}'))
        require(isinstance(self.conversations, dict), 'WW_CONVERSATIONS 必须为 JSON 对象')
        require(all(k in ('public', 'wolves', *(f'private:{p}' for p in SEATS)) and isinstance(v, str) and v
                    for k, v in self.conversations.items()), 'WW_CONVERSATIONS 映射无效')

    def handle(self, request):
        require(isinstance(request, dict) and request.get('jsonrpc') == '2.0', '无效 JSON-RPC 请求')
        method = request.get('method')
        params = request.get('params', {})
        require(isinstance(params, dict), 'params 必须为对象')
        if method == 'initialize':
            version = params.get('protocolVersion', '2024-11-05')
            return {'protocolVersion': version if version in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2024-11-05',
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'werewolf-engine', 'version': '1.1.0'}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': tool_list(self.actor)}
        if method == 'tools/call':
            try:
                require(set(params) <= {'name', 'arguments'} and isinstance(params.get('name'), str), '工具调用参数无效')
                result = self.store.call(self.actor, params['name'], params.get('arguments', {}),
                                         self.conversations)
                return {'content': [{'type': 'text', 'text': canonical(result)}], 'isError': False}
            except RuleError as exc:
                return {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
        raise RuleError('未知 JSON-RPC 方法')

    def run(self):
        for line in sys.stdin.buffer:
            request = None
            try:
                request = json.loads(line)
                if isinstance(request, dict) and 'id' not in request:
                    # Notifications, including initialized/cancelled, never mutate state.
                    continue
                result = self.handle(request)
                response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
            except (ValueError, TypeError, KeyError, RuleError) as exc:
                response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                            'error': {'code': -32600, 'message': str(exc) if isinstance(exc, RuleError) else '无效 JSON 请求'}}
            except Exception:
                # Do not leak raw events, paths or stack traces through player errors.
                response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                            'error': {'code': -32603, 'message': '内部错误；请求未获成功确认，请由运维核对日志'}}
            sys.stdout.write(canonical(response) + '\n')
            sys.stdout.flush()


def main():
    try:
        server = Server()
    except (RuleError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
