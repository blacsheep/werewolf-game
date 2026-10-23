<h1 align="center">AI Werewolf</h1>

<p align="center">
  <b>Twelve AI players and an AI God play Werewolf, then the game is voiced and turned into a replay.</b><br>
  English · <a href="README.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/blacsheep/werewolf-game/actions/workflows/test.yml"><img src="https://github.com/blacsheep/werewolf-game/actions/workflows/test.yml/badge.svg?branch=main" alt="test"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey" alt="Linux | macOS">
  <a href="https://github.com/blacsheep/agt"><img src="https://img.shields.io/badge/built%20on-agt-8A2BE2" alt="Built on agt"></a>
</p>

A 12-seat game (Seer, Witch, Hunter, Guard, four villagers, four wolves), hosted by an AI God and refereed by a rules engine. Tell the lead agent "开一局狼人杀" ("start a game of werewolf"), and the agents build the team, open the chats, host the game, voice it and render the video, with nobody watching. The game is played in Chinese. Built on [agt](https://github.com/blacsheep/agt).

<p align="center">
  <a href="docs/demo.mp4"><img src="docs/images/demo.webp" alt="God's view replay: highlights of game 1"></a><br>
  <a href="docs/demo.mp4">▶ Watch the voiced highlights</a> (3 minutes, God's view, in Chinese)
</p>

## Features

- **The God is an agent too.** It calls on players, announces deaths and runs the sheriff election and the exile votes in the same group chats and direct messages the players use. The engine decides every outcome; the God hosts from what it returns.
- **Each seat sees only its own information.** Every player has only its own seat's MCP server (`ww-p01` … `ww-p12`) and cannot read anyone else's role or night action. Sheriff sign-ups, withdrawals and votes go to the engine first; the God announces the results.
- **Every game can be audited.** Each action goes into a hash-chained event log, and roles come from a seed recorded in it. The replay and the video recompute the state from the log.
- **Models from different vendors can share a table.** Each seat can use a different model; the end of the game reveals which model played which seat.
- **Ready to watch.** Each game produces the full audio, a web replay switchable between the God's view and a player's view, and a God's-view mp4 as long as the audio.

![Player's view: the sheriff candidates speak](docs/images/replay-player.png)

## How a game runs

```text
you ── "开一局狼人杀" ──▶ lead (skill werewolf-game, tracked by a goal until done)
                          │
                          ├─ new_game (engine MCP ww-lead): game directory games/gNNN and seats.json
                          ├─ creates the God and 12 players, opens the public group, starts the game
                          │     God     ⇄ engine MCP ww-god
                          │     players ⇄ their own MCP ww-pXX
                          ├─ voice: tools/tts → transcript.json, audio/full.mp3, timeline.json
                          └─ replay: tools/replay → replay/index.html, replay/god.mp4
```

The game takes about two hours (measured with one reasoning model on all 13 seats); voicing and rendering take a few tens of minutes more.

## Installation

Linux and macOS are supported; 4 GB of RAM or more is recommended. Every agent runs on the models you configure in agt, so you do not need Codex or Claude Code. Windows is not supported: the engine relies on `fcntl` file locks and `O_NOFOLLOW`, and the scripts are bash.

| Requirement | Used for |
| --- | --- |
| [agt](https://github.com/blacsheep/agt) ≥ 0.6.1 | Every agent, plus the chats, tasks and web console |
| Python 3.10+ | The rules engine (standard library only) and the setup scripts |
| [uv](https://docs.astral.sh/uv/) | Runs the voice and replay tools, fetching edge-tts, numpy and Pillow on demand |
| ffmpeg | Joins the full game's audio and encodes the video |
| Noto Sans CJK fonts | Draws the Chinese text in the video |
| A model API with tool calling | At least one; the key goes in `models.env` |
| Network access to Microsoft's edge-tts | Voicing |
| Chrome or Chromium (optional) | The replay's automatic page check and the screen recorder; not needed to play |

<details>
<summary><b>Linux (Debian/Ubuntu)</b></summary>

```bash
# agt: a single prebuilt program in ~/.local/bin; run it again to upgrade
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/blacsheep/agt/releases/latest/download/agt-cli-installer.sh | sh
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install python3 ffmpeg fonts-noto-cjk

# Optional: Google Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
```

</details>

<details>
<summary><b>macOS</b> (needs <a href="https://brew.sh">Homebrew</a>)</summary>

```bash
# agt: a single prebuilt program in ~/.local/bin; run it again to upgrade
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/blacsheep/agt/releases/latest/download/agt-cli-installer.sh | sh
brew install python uv ffmpeg
brew install --cask font-noto-sans-cjk

# Optional: Google Chrome
brew install --cask google-chrome
```

</details>

If the fonts or Chrome are not in their usual places, point `WW_FONT` and `WW_FONT_BOLD` at the font files and `CHROME` at the browser.

## Quick start

```bash
git clone https://github.com/blacsheep/werewolf-game && cd werewolf-game
export AGT_HOME="$PWD/.agt"          # or let direnv load .envrc

cp models.env.example models.env     # fill in the URL, key and model of one or more vendors
scripts/setup-models.sh              # one agt model per filled-in block
scripts/setup-team.sh claude         # a model name from the previous step
scripts/console.sh &                 # start the web console; its link is in recording/console.log
```

A model's name is its block's prefix in `models.env`, in lowercase: `CLAUDE_*` becomes `claude`, and a block you add yourself, such as `LUNA_*`, becomes `luna`. `setup-models.sh` prints `ok <name> -> <model>` for each one. The model passed to `setup-team.sh` runs the lead, voice and replay, and is the default for the God and the players. Neither `models.env` nor `.agt/` is committed; keys stay in your local agt.

Open the console, message the lead and say "开一局狼人杀". The lead confirms the following in one message; reply "默认" ("defaults") to start:

- **Models**: the lead's model for the God and all 12 players by default, or one per seat.
- **Setup**: only the 12-seat game in [docs/rules.md](docs/rules.md) for now.
- **Outputs**: audio, replay and video by default.
- **Anything else**, such as how the players talk or how the God narrates.

The engine assigns the game id (the smallest unused one under `games/`: g001, g002, …). When it is done, the lead reports the winner, every role, where the outputs are and how the voice check went. For another game, say the same thing again.

> [!WARNING]
> `setup-team.sh` sets tool approval to **allow automatically** (`permissions.mode: auto_allow`): a game makes several hundred engine calls and would stall if each waited for approval. Voice and replay run commands on this machine as your user, outside any sandbox, without asking first. Run it only where you trust the models you use, or inside a virtual machine or container.

To approve calls instead, change "Native approval policy" in the web console under Settings → Tool approvals:

- **Model review, doubtful calls to you** (`model_review`): a reviewer model allows or denies each call and hands you the ones it is unsure about. The reviewer is already the model you passed to `setup-team.sh`, so it works as soon as you switch; it costs one more model call per tool call.
- **Manual** (`manual`): every call waits for you, so you stay with the game throughout; a request left for an hour cancels that run.

Running `setup-team.sh` again restores automatic approval. See [Permissions](https://blacsheep.github.io/agt/guide/permissions) and [Local execution](https://blacsheep.github.io/agt/guide/local-execution) in the agt docs.

## Outputs

| File | Contents |
| --- | --- |
| `games/<id>/events.jsonl` | the engine's hash-chained event log, the single source of truth |
| `games/<id>/seats.json` | the member and model of each seat |
| `games/<id>/transcript.json` | every line: public group, wolves' group, direct messages, narration |
| `games/<id>/audio/full.mp3` | the full game's audio |
| `games/<id>/timeline.json` | when each line starts and ends in the audio |
| `games/<id>/replay/index.html` | the web replay; open it in a browser |
| `games/<id>/replay/god.mp4` | the God's-view video |

### Sample game

[examples/sample-game](examples/sample-game) holds the event log, seats, transcript and timeline of a complete game (the wolves win on day 4). Watch its web replay without configuring any model (silent; the subtitles keep the original timing):

```bash
scripts/sample-replay.sh    # builds games/sample/replay/index.html
```

### Rebuilding the audio and replay by hand

Both tools work on `games/current` (the latest game) unless `--game` names another. Pass the public group id only the first time a game is voiced; it is then read from the game's `tts.json`. The wolves' group is recorded by the engine.

```bash
tools/tts/run.sh --game games/g001 [--public <public group id>]
uv run --with pillow python tools/replay/build.py --game games/g001
```

See [engine/README.md](engine/README.md), [tools/tts/README.md](tools/tts/README.md) and [tools/replay/README.md](tools/replay/README.md) for details (in Chinese).

## Development

```text
engine/              rules engine: one MCP server, one locked instance per seat
prompts/             prompts for the God, the players, voice and replay
docs/rules.md        the setup and rules every agent and the engine follow
docs/playbook.md     strategy notes for players
docs/skills/         werewolf-game for the lead, ww-playbook for the players
docs/brief.md        the staged requirements the lead was given to build this project (its code was written by agents too)
tools/tts/           voices: merge the chats, synthesize with edge-tts, join, verify
tools/replay/        replay: web page, public audio track, God's-view mp4
scripts/             setup, console, sample replay, screen recording
examples/            a sample game
```

The tests use no network and call no model:

```bash
python3 -m unittest discover -s engine/tests
uv run --no-project --with numpy python3 -m unittest discover -s tools/tts/tests
cd tools/replay && uv run --no-project --with pillow python -m unittest test_replay
```

The replay tests need a game with its full audio, `games/current` by default, and are skipped without one; set `WW_REPLAY_GAME` to use another, such as the `games/sample` that `scripts/sample-replay.sh` builds.

<details>
<summary><b>Screen recording</b></summary>

```bash
uv run --with playwright python scripts/record.py --name run1   # a console screenshot every 3 s
echo <conversation id> > recording/focus                          # switch to a conversation
touch recording/stop                                               # stop
scripts/make-video.sh run1                                         # a sped-up mp4 and webp
```

</details>

## Known limits

- There is one setup; another needs changes to the engine, the rules and the prompts.
- edge-tts uses Microsoft's online read-aloud service, not a public API, and may be rate-limited or change. It has only 11 Mandarin voices, so some seats differ only in pitch and speed.
- Models sometimes break the table rules in what they say, such as speaking too long or opening a private group. The engine refuses illegal actions but cannot control what a model says.
- A game makes a great many model calls; estimate the cost before you start.

## Acknowledgements

Thanks to the [LINUX DO](https://linux.do) community for its support and discussions.

## License

[MIT](LICENSE)
