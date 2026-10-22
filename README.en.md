# AI Werewolf

English | [中文](README.md)

Twelve AI players play a 12-seat Werewolf game (Seer, Witch, Hunter, Guard, four villagers, four wolves), hosted by an AI God. When the game ends it is voiced, and turned into a replay and a video you can watch from the God's view or a player's view. The game is played in Chinese.

You tell the lead agent "开一局狼人杀" ("start a game of werewolf") and answer one setup question. The agents do the rest: they build the team, open the chats, host the game, record the voices and render the video, with nobody watching. Built on [agt](https://github.com/blacsheep/agt).

[![God's view replay: highlights of game 1](docs/images/demo.webp)](docs/demo.mp4)

[▶ Watch the voiced highlights](docs/demo.mp4) (3 minutes, God's view, in Chinese): the wolves pick their first target, wolf No. 1 claims to be the Seer and wins the sheriff badge, the real Seer No. 5 is exiled, and the wolves win.

## What's different

- **The God is an agent too.** It calls on players, announces deaths and runs the sheriff election and the exile votes, in the same group chats and direct messages the players use. The rules engine decides every outcome; the God hosts from what the engine returns.
- **Each seat sees only its own information.** Every player has only its own seat's MCP server (`ww-p01` … `ww-p12`), so it cannot read anyone else's role or night action. Running for sheriff, withdrawing and voting are submitted to the engine and then told to the God in private; the God announces the results.
- **Every game can be audited and replayed.** The engine writes each action to a hash-chained event log, and roles come from a seed recorded in that log. The replay and the video recompute the state from the log, not from what the models said.
- **Models from different vendors can share a table.** Each seat can use a different model; the end of the replay reveals which model played which seat.
- **The results are ready to watch.** Each game produces the full audio, a web replay with subtitles and seat highlights (switchable between the God's view and a player's view), and a God's-view mp4 as long as the audio.

![Player's view: the sheriff candidates speak](docs/images/replay-player.png)

## How a game runs

```text
you ── "开一局狼人杀" ──▶ lead (skill werewolf-game, tracked by a goal until done)
                          │
                          ├─ coding agent: game directory games/gNNN and seats.json
                          ├─ creates the God and 12 players, opens the public group, starts the game
                          │     God     ⇄ engine MCP ww-god
                          │     players ⇄ their own MCP ww-pXX
                          ├─ voice agent: tools/tts → transcript.json, audio/full.mp3, timeline.json
                          └─ replay agent: tools/replay → replay/index.html, replay/god.mp4
```

The game itself takes about two hours (measured with one reasoning model on all 13 seats); voicing and rendering take a few tens of minutes more.

## Requirements

- [agt](https://github.com/blacsheep/agt) ≥ 0.6.1
- Python 3.10+, [uv](https://docs.astral.sh/uv/), ffmpeg
- Two coding harnesses: [Codex CLI](https://github.com/openai/codex) and [Claude Code](https://claude.com/claude-code)
- Google Chrome or Chromium (replay checks and screen recording)
- Noto Sans CJK fonts (Debian/Ubuntu: `apt install fonts-noto-cjk`; point `WW_FONT` and `WW_FONT_BOLD` at other font files)
- At least one model API with tool calling
- Network access to Microsoft's edge-tts service (for the voices)
- 4 GB of RAM or more is recommended

Pick the steps for your system. On every system you then sign in to the two coding harnesses: run `codex login` once, and Claude Code asks you to sign in the first time you run `claude`. `scripts/setup-team.sh` tests both harnesses when it registers them, so sign in before running it.

### Linux (Debian/Ubuntu)

```bash
# agt: a prebuilt binary in ~/.local/bin; run it again to upgrade an older version
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/blacsheep/agt/releases/latest/download/agt-cli-installer.sh | sh
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install python3 ffmpeg fonts-noto-cjk nodejs npm
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
npm install -g @openai/codex
curl -fsSL https://claude.ai/install.sh | bash
```

### macOS

Install [Homebrew](https://brew.sh) first.

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/blacsheep/agt/releases/latest/download/agt-cli-installer.sh | sh
brew install python uv ffmpeg node
brew install --cask google-chrome font-noto-sans-cjk
npm install -g @openai/codex
curl -fsSL https://claude.ai/install.sh | bash
```

If the replay cannot find the Chinese font, point `WW_FONT` and `WW_FONT_BOLD` at the font files; if Chrome is not in its usual place, point `CHROME` at the browser binary.

### Windows

The engine and scripts rely on POSIX file locks, symbolic links and bash, so they do not run on native Windows. Use WSL2:

```powershell
wsl --install -d Ubuntu
```

After the restart, open Ubuntu and follow the Linux steps above inside it; run every later command in WSL as well. Clone the project into WSL's own file system (such as `~/werewolf-game`), not under `/mnt/c`: files there are open to every user, and the engine refuses to read or write a game log with those permissions. Open the console link in your Windows browser.

## Quick start

```bash
git clone https://github.com/blacsheep/werewolf-game && cd werewolf-game
export AGT_HOME="$PWD/.agt"          # or let direnv load .envrc

cp models.env.example models.env     # fill in the URL, key and model of one or more vendors
scripts/setup-models.sh              # one agt model per filled-in vendor, named after it, e.g. claude
scripts/setup-team.sh claude         # the lead runs on this model; it is also the default for the God and players
scripts/console.sh &                 # start the web console; its link is written to recording/console.log
```

`models.env` is gitignored, and keys are stored only in your local agt providers. Everything under `.agt/` is generated for your machine by the setup scripts and is not committed either.

Open the console, message the lead and say "开一局狼人杀". The lead asks you once about the following; reply "默认" ("defaults") to start right away:

1. Game id: the next unused one by default.
2. The model for each seat: the lead's model for the God and all 12 players by default, or one per seat.
3. The setup: only the 12-seat game in [docs/rules.md](docs/rules.md) is supported for now.
4. Which outputs you want: audio, replay and video by default.
5. Anything else, such as how the players talk or how the God narrates.

When it is done, the lead reports the winner, every role, where the outputs are and how the voice check went. For another game, say the same thing again.

## What each game produces

| File | Contents |
| --- | --- |
| `games/<id>/events.jsonl` | the engine's hash-chained event log, the single source of truth |
| `games/<id>/seats.json` | the member and model of each seat |
| `games/<id>/transcript.json` | every line: public group, wolves' group, direct messages, narration |
| `games/<id>/audio/full.mp3` | the full game's audio |
| `games/<id>/timeline.json` | when each line starts and ends in the audio |
| `games/<id>/replay/index.html` | the web replay; open it in a browser |
| `games/<id>/replay/god.mp4` | the God's-view video |

[examples/sample-game](examples/sample-game) holds the event log, seats, transcript and timeline of a complete game (the wolves win on day 4), so you can read one through without running any model.

You can also rebuild the audio and the replay by hand:

```bash
tools/tts/run.sh --public <public group id>
uv run --with pillow python tools/replay/build.py --game games/g001
```

See [engine/README.md](engine/README.md), [tools/tts/README.md](tools/tts/README.md) and [tools/replay/README.md](tools/replay/README.md) for details (in Chinese).

## Layout

```text
engine/              rules engine: one MCP server, one locked instance per seat
prompts/             prompts for the God, the players and the three coding agents
docs/rules.md        the setup and rules every agent and the engine follow
docs/playbook.md     strategy notes for players
docs/skills/         werewolf-game for the lead, ww-playbook for the players
docs/brief.md        the staged requirements the lead was given to build this project
tools/tts/           voices: merge the chats, synthesize with edge-tts, join, verify
tools/replay/        replay: web page, public audio track, God's-view mp4
scripts/             setup, console, screen recording
examples/            a sample game
```

## Tests

```bash
python3 -m unittest discover -s engine/tests
uv run --no-project --with numpy python3 -m unittest discover -s tools/tts/tests
cd tools/replay && uv run --no-project --with pillow python -m unittest test_replay
```

None of them use the network or call a model. The replay tests need a finished game with its full audio, `games/current` by default, and are skipped without one; set `WW_REPLAY_GAME` to use another.

## Screen recording (optional)

```bash
uv run --with playwright python scripts/record.py --name run1   # a console screenshot every 3 s
echo <conversation id> > recording/focus                          # switch to a conversation
touch recording/stop                                               # stop
scripts/make-video.sh run1                                         # a sped-up mp4 and webp
```

## Known limits

- There is one setup. Another one needs changes to the engine, the rules and the prompts.
- edge-tts uses Microsoft's online read-aloud service, not a public API, and may be rate-limited or change at any time. It has only 11 Mandarin voices, so some seats differ only in pitch and speed.
- Models sometimes break the table rules in what they say, such as speaking too long or opening a private group. The engine refuses illegal actions but cannot control what a model says.
- A game makes a great many model calls: every speech, vote and night action of every seat, and every step of the God. Estimate the cost for your models before you start.

## Built by agents

The engine, the voice pipeline and the replay tools were written by coding agents in agt, stage by stage, from [docs/brief.md](docs/brief.md); people only gave the requirements and checked the results.

## License

[MIT](LICENSE)
