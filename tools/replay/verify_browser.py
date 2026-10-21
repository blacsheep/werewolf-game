#!/usr/bin/env python3
"""Verify the delivered HTML through real Chrome, saving selected screenshots."""
import argparse
import hashlib
import json
from pathlib import Path
from browser import Browser
from build import ROOT, check_memory, load_game, write


def verify(game):
    data = load_game(game)
    entries = data['entries']
    output = game / 'replay'
    shots = output / 'screenshots'
    shots.mkdir(exist_ok=True)
    samples = {
        'election': next(e for e in entries if '警上' in e['phase_label'] and e['speaker'].startswith('p')),
        'wolves': next(e for e in entries if e['channel'] == 'wolves' and e['speaker'].startswith('p')),
        'eliminated': next(e for e in entries if len(e['state']['alive']) < 12),
        'final': entries[-1],
    }
    report = {'screenshots': [], 'checked_entries': {}, 'checks': [],
              'html_sha256': hashlib.sha256((output / 'index.html').read_bytes()).hexdigest()}
    check_memory(700_000_000)
    with Browser() as browser:
        report['browser'] = browser.call('Browser.getVersion')['product']
        browser.call('Network.enable')
        browser.call('Network.setBlockedURLs', {'urls': ['*full.mp3', '*public.mp3']})
        browser.open(output / 'index.html')
        assert browser.evaluate('audio.preload') == 'none'

        def inspect(e, mode):
            observed = browser.evaluate('replay.inspect()')
            assert observed['id'] == e['id'], (e['id'], observed['id'], e['start'], observed['position'], mode)
            assert observed['subtitle'] == e['text']
            assert observed['phase'] == e['phase_label']
            assert observed['active'] == ([e['speaker']] if e['speaker'].startswith('p') else [])
            assert observed['dead'] == [p['id'] for p in data['players'] if p['id'] not in e['state']['alive']]
            assert observed['sheriff'] == ([e['state']['sheriff']] if e['state']['sheriff'] else [])
            assert observed['candidates'] == e['state']['candidates']
            assert observed['roles'] == [p['role'] if mode == 'god' or e['state']['winner'] else '身份未公开' for p in data['players']]
            assert observed['models'] == [p['model_label'] if e['state']['winner'] else '' for p in data['players']]
            assert observed['audio'] == ('public.mp3' if mode == 'player' else '../audio/full.mp3')
            assert observed['duration'] == data['public_duration' if mode == 'player' else 'duration']
            assert observed['subtitleFits'], ('字幕溢出', e['id'], mode)
            if mode == 'player':
                assert observed['channel'] == 'public'
            return observed

        for name, original in samples.items():
            for mode in ('god', 'player'):
                browser.evaluate(f"replay.seekSource({original['start']}, '{mode}')")
                e = next((row for row in entries if row['end'] > original['start'] and (mode == 'god' or row['channel'] == 'public')), entries[-1])
                observed = inspect(e, mode)
                if original['channel'] != 'public' and mode == 'player':
                    assert original['text'] not in observed['bodyText']
                report['checked_entries'][mode] = report['checked_entries'].get(mode, 0) + 1
                screenshot_mode = 'god' if name == 'wolves' else 'player'
                if mode != screenshot_mode:
                    continue
                path = shots / f'{name}-{mode}.png'
                browser.screenshot(path)
                report['screenshots'].append({'path': str(path.relative_to(output)), 'requested_source_seconds': original['start'], 'source_seconds': e['start'], 'perspective': mode, 'id': e['id'], 'phase': e['phase_label'], 'speaker': e['speaker'], 'dead': observed['dead'], 'sheriff': observed['sheriff'], 'subtitle_fits': observed['subtitleFits']})

        # Check every private sentence, including each role's private channel.
        report['private_entries'] = []
        for private in (e for e in entries if e['channel'] != 'public'):
            browser.evaluate(f"replay.seekSource({private['start']}, 'god')")
            inspect(private, 'god')
            browser.evaluate("document.getElementById('playerView').click()")
            next_public = next(e for e in entries if e['start'] >= private['start'] and e['channel'] == 'public')
            observed = inspect(next_public, 'player')
            assert private['text'] not in observed['bodyText']
            report['private_entries'].append({'id': private['id'], 'channel': private['channel'],
                                              'next_public_id': next_public['id']})

        # Exercise actual controls without loading or decoding the full audio.
        private = samples['wolves']
        browser.evaluate(f"replay.seekSource({private['start']}, 'god')")
        browser.evaluate("document.getElementById('playerView').click()")
        next_public = next(e for e in entries if e['start'] >= private['start'] and e['channel'] == 'public')
        observed = inspect(next_public, 'player')
        assert private['text'] not in observed['bodyText']
        controls = browser.evaluate("({max:Number($('progress').max),value:Number($('progress').value),button:$('play').textContent})")
        assert controls['max'] == data['public_duration']
        assert abs(controls['value'] - observed['position']) < .001
        assert controls['button'] == '播放'
        browser.evaluate("document.getElementById('next').click()")
        visible = [e for e in entries if e['channel'] == 'public']
        inspect(visible[visible.index(next_public)+1], 'player')
        browser.evaluate("document.getElementById('previous').click()")
        inspect(next_public, 'player')
        browser.evaluate("$('progress').value=0;$('progress').dispatchEvent(new Event('input'))")
        inspect(entries[0], 'player')
        report['audio_check'] = browser.evaluate("({preload:audio.preload,readyState:audio.readyState,buffered:audio.buffered.length})")
        assert report['audio_check'] == {'preload': 'none', 'readyState': 0, 'buffered': 0}
        report['checks'] += ['four_scenes_both_views_dom', 'all_private_entries_both_views', 'private_to_public_switch',
                             'public_audio_selected', 'next_previous_seek', 'audio_loading_blocked']
    report['passed'] = True
    write(output / 'browser-report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path, default=ROOT/'games/current')
    verify(parser.parse_args().game.resolve())
