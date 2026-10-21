"""Read full group history through the agt CLI as web-operator (no fabricated fallback)."""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class FetchError(RuntimeError):
    pass


def call_history(conversation, before=None, agt='agt', home=ROOT / '.agt', limit=200):
    args = {'conversation': conversation, 'limit': limit}
    if before:
        args['before'] = before
    env = dict(os.environ, AGT_HOME=str(home))
    proc = subprocess.run([agt, '--output', 'json', 'tool', 'call', '--principal', 'web-operator',
                           'message_history', '--args', json.dumps(args)],
                          capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        raise FetchError(f'agt message_history 失败（{conversation}，退出码 {proc.returncode}）：'
                         f'{proc.stderr.strip()[:500]}')
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(f'agt 输出不是 JSON（{conversation}）：{proc.stdout[:200]}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
        raise FetchError(f'agt 返回结构异常（{conversation}）：{str(data)[:200]}')
    return data


def fetch_all(conversation, **kwargs):
    """Follow next_cursor until exhausted. Returns (messages sorted by seq, pages)."""
    seen, pages, cursors, before = {}, 0, set(), None
    while True:
        data = call_history(conversation, before=before, **kwargs)
        pages += 1
        for m in data['messages']:
            if m.get('conversation_id') not in (None, conversation):
                raise FetchError(f'返回了其他会话的消息：{m.get("conversation_id")}')
            seen[m['id']] = m
        before = data.get('next_cursor')
        if not before:
            break
        if before in cursors or pages > 500:
            raise FetchError(f'翻页游标重复或页数异常（{conversation}），停止以免死循环')
        cursors.add(before)
    messages = sorted(seen.values(), key=lambda m: m['seq'])
    seqs = [m['seq'] for m in messages]
    gaps = sorted(set(range(seqs[0], seqs[-1] + 1)) - set(seqs)) if seqs else []
    return messages, {'pages': pages, 'count': len(messages), 'seq_gaps': gaps,
                      'first_seq': seqs[0] if seqs else None, 'last_seq': seqs[-1] if seqs else None}


def save_raw(path, conversation, messages, info):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'conversation': conversation, 'fetch': info, 'messages': messages}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_raw(path):
    data = json.loads(Path(path).read_text())
    return data['messages'], data['fetch']
