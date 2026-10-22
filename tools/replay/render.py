"""Render one 1280×720 god-view subtitle page at a time, without a browser."""
import math
import os
import random
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Noto Sans CJK in the usual Linux and macOS locations, then whatever fontconfig finds;
# WW_FONT and WW_FONT_BOLD name the files directly.
FONT_DIRS = ['/usr/share/fonts/opentype/noto', '/usr/share/fonts/noto-cjk', '/usr/share/fonts/google-noto-cjk',
             '/usr/share/fonts/OTF', '/opt/homebrew/share/fonts', '/Library/Fonts', str(Path.home() / 'Library/Fonts')]
W, H = 1280, 720
INK, MUTED, FAINT = '#f3efe6', '#a9adbb', '#6c7184'
WOLF, GOD_ROLE, VILLAGER, GOLD, PRIVATE = '#e5484d', '#f5b84a', '#5b9cf5', '#f5c451', '#a78bfa'
CAMPS = {'狼人': WOLF, '村民': VILLAGER, '预言家': GOD_ROLE, '女巫': GOD_ROLE, '猎人': GOD_ROLE, '守卫': GOD_ROLE}
CENTER, RX, RY = (640, 292), 468, 178
SUBTITLE_SIZE, SUBTITLE_WIDTH, SUBTITLE_LINES = 30, 1080, 2
CAPTION_BOX = (40, 540, 1240, 700)
BREAKS = re.compile(r'(?<=[。！？；…，、：])')
NO_LINE_START = set('，。；：、！？…）》」』”')


@lru_cache(maxsize=2)
def font_file(bold):
    weight = 'Bold' if bold else 'Regular'
    if os.environ.get('WW_FONT_BOLD' if bold else 'WW_FONT'):
        return os.environ['WW_FONT_BOLD' if bold else 'WW_FONT']
    for folder in FONT_DIRS:
        if (path := Path(folder) / f'NotoSansCJK-{weight}.ttc').exists():
            return str(path)
    try:
        found = subprocess.run(['fc-match', '-f', '%{file}', f'Noto Sans CJK SC:style={weight}'],
                               capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        found = ''
    if 'CJK' not in found:
        raise RuntimeError('找不到中文字体 Noto Sans CJK：请安装（Debian/Ubuntu：apt install fonts-noto-cjk；macOS：brew install --cask font-noto-sans-cjk），'
                           '或用 WW_FONT、WW_FONT_BOLD 指定字体文件')
    return found


@lru_cache(maxsize=32)
def font(size, bold=False):
    return ImageFont.truetype(font_file(bold), size)


def wrap(text, size, width, bold=False):
    lines, line = [], ''
    face = font(size, bold)
    for char in text:
        if char == '\n':
            lines.append(line)
            line = ''
        elif line and face.getlength(line + char) > width and char not in NO_LINE_START:
            # Punctuation never starts a line; it may hang past the width instead.
            lines.append(line)
            line = char
        else:
            line += char
    lines.append(line)
    return lines


def fits(text):
    return len(wrap(text, SUBTITLE_SIZE, SUBTITLE_WIDTH)) <= SUBTITLE_LINES


def pages(text):
    """Split a line into subtitle pages of at most two lines, breaking after punctuation."""
    result, page = [], ''
    for clause in (c for c in BREAKS.split(text.replace('\n', '')) if c):
        if fits(page + clause):
            page += clause
            continue
        if page:
            result.append(page)
            page = ''
        while not fits(clause):
            # A clause longer than a page is cut at the last character that still fits.
            cut = max(i for i in range(1, len(clause) + 1) if fits(clause[:i]))
            result.append(clause[:cut])
            clause = clause[cut:]
        page = clause
    if page or not result:
        result.append(page)
    return result


def cues(entry):
    """Subtitle pages of an entry with times, spread over the spoken part by length."""
    texts = pages(entry['text'])
    spoken = entry.get('voice_end', entry['end']) - entry['start']
    total = sum(len(t) for t in texts) or 1
    result, at = [], entry['start']
    for i, text in enumerate(texts):
        end = entry['end'] if i == len(texts) - 1 else round(at + spoken * len(text) / total, 3)
        result.append({'start': at, 'end': end, 'text': text, 'index': i, 'count': len(texts)})
        at = end
    return result


def channel_label(channel):
    if channel == 'public':
        return '公开', '#8b93a8'
    if channel == 'wolves':
        return '狼队夜聊', WOLF
    return f"{int(channel.split(':p')[1])}号私聊", PRIVATE


def seat_position(i):
    angle = math.radians(i * 30 - 90)
    return CENTER[0] + RX * math.cos(angle), CENTER[1] + RY * math.sin(angle)


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


@lru_cache(maxsize=2)
def backdrop(night):
    top, bottom = (rgb('#090d1f'), rgb('#1b1840')) if night else (rgb('#17130f'), rgb('#2a211a'))
    image = Image.new('RGB', (W, H))
    draw = ImageDraw.Draw(image)
    for y in range(H):
        k = y / (H - 1)
        draw.line((0, y, W, y), fill=tuple(round(top[j] + (bottom[j] - top[j]) * k) for j in range(3)))
    if night:
        stars = random.Random(7)
        for _ in range(90):
            x, y, r = stars.randrange(W), stars.randrange(76, 530), stars.choice((1, 1, 1, 2))
            draw.ellipse((x - r / 2, y - r / 2, x + r / 2, y + r / 2), fill=stars.choice(('#3b4270', '#545c94', '#7a83c2')))
    table = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    box = (CENTER[0] - RX + 70, CENTER[1] - RY + 36, CENTER[0] + RX - 70, CENTER[1] + RY - 36)
    ImageDraw.Draw(table).ellipse(box, fill=(34, 40, 74, 150) if night else (60, 47, 34, 150),
                                  outline=(255, 255, 255, 28), width=2)
    image.paste(table, (0, 0), table)
    return image


def glow(image, x, y, radius, color):
    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((x - radius, y - radius, x + radius, y + radius), fill=rgb(color) + (150,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius / 2.5))
    image.paste(layer, (0, 0), layer)


def moon(draw, x, y, r):
    draw.ellipse((x - r, y - r, x + r, y + r), fill='#c9d1ff')
    draw.ellipse((x - r * .45, y - r * 1.25, x + r * 1.55, y + r * .75), fill='#0d1128')


def sun(draw, x, y, r):
    draw.ellipse((x - r * .55, y - r * .55, x + r * .55, y + r * .55), fill='#ffcf6b')
    for k in range(8):
        a = math.radians(k * 45)
        draw.line((x + r * .8 * math.cos(a), y + r * .8 * math.sin(a),
                   x + r * 1.15 * math.cos(a), y + r * 1.15 * math.sin(a)), fill='#ffcf6b', width=2)


def speaker_style(data, entry):
    """Name, tag and colour for the speaker of an entry."""
    player = next((p for p in data['players'] if p['id'] == entry['speaker']), None)
    if player:
        return entry['speaker_name'], player['role'], CAMPS[player['role']]
    if entry['speaker'] == 'god':
        return '主持人', '上帝', GOLD
    return '旁白', '上帝视角', MUTED


def render_frame(data, entry, path, cue=None):
    cue = cue or {'text': entry['text'].replace('\n', ''), 'index': 0, 'count': 1}
    state = entry['state']
    night = '夜' in entry['phase_label'] and not state['winner']
    image = backdrop(night).copy()
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=16, fill=INK, anchor='lt', bold=False):
        draw.text((x, y), value, font=font(size, bold), fill=fill, anchor=anchor)

    def pill(x, y, label, color, size=14, anchor='l', filled=False):
        width = font(size, True).getlength(label) + 22
        left = x if anchor == 'l' else x - width
        box = (left, y, left + width, y + size + 14)
        draw.rounded_rectangle(box, radius=(size + 14) / 2, fill=color if filled else None,
                               outline=color, width=2)
        text(left + width / 2, y + (size + 14) / 2, label, size, '#141414' if filled else color, 'mm', True)
        return box

    # Header: title, phase, and how many of each camp are still alive.
    text(40, 20, 'AI 狼人杀', 26, INK, bold=True)
    text(176, 31, f"{data['game_id'].upper()} · 12人预女猎守 · 上帝视角", 13, MUTED)
    label_width = font(22, True).getlength(entry['phase_label'])
    left = 640 - label_width / 2 + 14
    (moon if night else sun)(draw, left - 24, 36, 11)
    text(left, 36, entry['phase_label'], 22, INK, 'lm', True)
    alive = set(state['alive'])
    living = [p for p in data['players'] if p['id'] in alive]
    counts = [('狼', WOLF, sum(p['role'] == '狼人' for p in living)),
              ('神', GOD_ROLE, sum(p['role'] not in ('狼人', '村民') for p in living)),
              ('民', VILLAGER, sum(p['role'] == '村民' for p in living))]
    x = 1240
    for label, color, count in reversed(counts):
        x = pill(x, 21, f'{label} {count}', color, 14, 'r')[0] - 8
    draw.line((40, 70, 1240, 70), fill='#2c2f45' if night else '#3a3029')

    # Seats around the table, coloured by camp.
    seats = []
    for i, player in enumerate(data['players']):
        x, y = seat_position(i)
        dead, active = player['id'] not in alive, entry['speaker'] == player['id']
        sheriff = state['sheriff'] == player['id']
        camp = CAMPS[player['role']]
        r = 34 if active else 28
        if active:
            glow(image, x, y, 60, camp)
            draw = ImageDraw.Draw(image)
        draw.ellipse((x - r, y - r, x + r, y + r), fill='#24252e' if dead else '#151827' if night else '#221b15',
                     outline=FAINT if dead else INK if active else camp, width=4 if active else 3)
        text(x, y - 2, str(player['number']), 28 if active else 24, FAINT if dead else INK, 'mm', True)
        if dead:
            draw.line((x - r * .55, y + r * .55, x + r * .55, y - r * .55), fill='#9a4044', width=3)
        text(x, y + r + 6, player['role'] + (' · 出局' if dead else ''), 14, FAINT if dead else camp, 'mt', True)
        model = player['model_label'] if state['winner'] else ''
        if model:
            if font(12).getlength(model) > 150:
                raise ValueError('模型名称超出座位宽度')
            text(x, y + r + 27, model, 12, MUTED, 'mt')
        if player['id'] in state.get('candidates', []):
            # Running for sheriff: a gold tag on the upper left.
            bx, by = x - r * .95, y - r * .8
            draw.rounded_rectangle((bx - 19, by - 10, bx + 19, by + 10), radius=10, fill='#141414', outline=GOLD, width=2)
            text(bx, by - 1, '警上', 11, GOLD, 'mm', True)
        if sheriff:
            bx, by = x + r * .78, y - r * .78
            draw.ellipse((bx - 12, by - 12, bx + 12, by + 12), fill=GOLD, outline='#141414', width=2)
            text(bx, by - 1, '警', 13, '#141414', 'mm', True)
        seats.append({'id': player['id'], 'dead': dead, 'active': active, 'sheriff': sheriff,
                      'candidate': player['id'] in state.get('candidates', []),
                      'role': player['role'], 'model': model})

    # Centre stage: the speaker, or the result once the game is over.
    cx, cy = CENTER
    channel, channel_color = channel_label(entry['channel'])
    name, tag, color = speaker_style(data, entry)
    if state['winner']:
        good = state['winner'] == 'good'
        text(cx, cy - 36, '游戏结束', 18, MUTED, 'mm')
        text(cx, cy + 6, '好人阵营获胜' if good else '狼人阵营获胜', 46, VILLAGER if good else WOLF, 'mm', True)
    else:
        text(cx, cy - 30, name, 40, INK, 'mm', True)
        width = font(14, True).getlength(tag) + font(14, True).getlength(channel) + 52
        box = pill(cx - width / 2, cy + 8, tag, color, 14)
        pill(box[2] + 8, cy + 8, channel, channel_color, 14, filled=entry['channel'] != 'public')

    # Subtitle card: one page of the line at a time.
    card = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle(CAPTION_BOX, radius=16, fill=(8, 10, 20, 210))
    image.paste(card, (0, 0), card)
    draw = ImageDraw.Draw(image)
    accent = channel_color if entry['channel'] != 'public' else color
    draw.rounded_rectangle((CAPTION_BOX[0], CAPTION_BOX[1], CAPTION_BOX[0] + 6, CAPTION_BOX[3]), radius=3, fill=accent)
    text(68, 556, name, 16, accent, bold=True)
    text(68 + font(16, True).getlength(name) + 12, 558, channel, 14, MUTED)
    if cue['count'] > 1:
        text(1212, 558, f"{cue['index'] + 1} / {cue['count']}", 14, FAINT, 'rt')
    lines = wrap(cue['text'], SUBTITLE_SIZE, SUBTITLE_WIDTH)
    if len(lines) > SUBTITLE_LINES:
        raise ValueError('字幕无法完整放入视频画面')
    for i, line in enumerate(lines):
        text(68, 592 + i * 46, line, SUBTITLE_SIZE, '#d6d9e4' if entry['speaker'] == 'narrator' else INK)
    try:
        image.save(path, compress_level=1)
    finally:
        image.close()
    return {'renderer': 'Pillow', 'size': [W, H], 'subtitle_font_size': SUBTITLE_SIZE,
            'subtitle_lines': lines, 'seats': seats}
