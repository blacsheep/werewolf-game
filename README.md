<h1 align="center">AI 狼人杀</h1>

<p align="center">
  <b>12 个 AI 玩家、1 个 AI 上帝，打完自动配音，生成可回放的对局。</b><br>
  <a href="README.en.md">English</a> · 简体中文
</p>

<p align="center">
  <a href="https://github.com/blacsheep/werewolf-game/actions/workflows/test.yml"><img src="https://github.com/blacsheep/werewolf-game/actions/workflows/test.yml/badge.svg?branch=main" alt="test"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey" alt="Linux | macOS">
  <a href="https://github.com/blacsheep/agt"><img src="https://img.shields.io/badge/built%20on-agt-8A2BE2" alt="Built on agt"></a>
</p>

12 人预女猎守，由 AI 上帝主持，规则引擎裁决。你对 lead 说一句「开一局狼人杀」，组队、建群、主持、配音和出视频都由 agent 完成，中途不用人盯。项目基于 [agt](https://github.com/blacsheep/agt)。

<p align="center">
  <a href="docs/demo.mp4"><img src="docs/images/demo.webp" alt="上帝视角回放：第 1 局精华"></a><br>
  <a href="docs/demo.mp4">▶ 观看带语音的精华剪辑</a>（3 分钟，上帝视角）
</p>

## 特点

- **上帝也是 agent。** 点名、宣布死讯、组织警选和放逐都由上帝在群聊和私聊里完成；结果由引擎裁决，上帝只依据引擎返回的结果主持。
- **信息隔离。** 每名玩家只挂自己座位的 MCP（`ww-p01` … `ww-p12`），拿不到别人的身份和夜间行动。上警、退水和投票先提交给引擎，再由上帝统一公布。
- **可审计、可复盘。** 每个动作写进带哈希链的事件日志，身份由日志里记录的种子分配；回放和视频按日志重算状态。
- **多家模型同台。** 每个座位可以用不同厂商的模型，终局揭晓谁是哪个模型。
- **产出可以直接看。** 每局生成整局语音、可切换上帝视角和玩家视角的网页回放，以及和音频等长的上帝视角 mp4。

![玩家视角：警上发言](docs/images/replay-player.png)

## 工作流程

```text
你 ──「开一局狼人杀」──▶ lead（skill: werewolf-game，用 goal 跟踪到完成）
                          │
                          ├─ new_game（引擎 MCP ww-lead）：建对局目录 games/gNNN 和座位表 seats.json
                          ├─ 创建上帝和 12 名玩家，建公共群，开局
                          │     上帝 ⇄ 引擎 MCP ww-god
                          │     玩家 ⇄ 各自的 MCP ww-pXX
                          ├─ voice：tools/tts → transcript.json、audio/full.mp3、timeline.json
                          └─ replay：tools/replay → replay/index.html、replay/god.mp4
```

对局部分约两小时（13 个座位都用同一个推理模型实测），配音和出视频再要几十分钟。

## 安装

支持 Linux 和 macOS，内存建议 4 GB 以上。所有 agent 都运行在 agt 里你配置的模型上，不需要 Codex 或 Claude Code。Windows 暂不支持：引擎依赖 `fcntl` 文件锁和 `O_NOFOLLOW` 等接口，脚本也都是 bash。

| 依赖 | 用途 |
| --- | --- |
| [agt](https://github.com/blacsheep/agt) ≥ 0.6.1 | 运行全部 agent，以及群聊、任务和 Web 控制台 |
| Python 3.10+ | 规则引擎（只用标准库）和 setup 脚本 |
| [uv](https://docs.astral.sh/uv/) | 运行配音和回放工具，按需拉取 edge-tts、numpy、Pillow |
| ffmpeg | 拼接整局音频，编码回放视频 |
| Noto Sans CJK 字体 | 在视频画面上绘制中文 |
| 支持工具调用的模型 API | 至少一个，key 填在 `models.env` |
| 能访问微软 edge-tts 的网络 | 配音 |
| Chrome 或 Chromium（可选） | 回放网页的自动验收和录屏，开局用不到 |

<details>
<summary><b>Linux（Debian/Ubuntu）</b></summary>

```bash
# agt：预编译的单个程序，装到 ~/.local/bin；重跑即升级
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/blacsheep/agt/releases/latest/download/agt-cli-installer.sh | sh
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install python3 ffmpeg fonts-noto-cjk

# 可选：Google Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
```

</details>

<details>
<summary><b>macOS</b>（需要 <a href="https://brew.sh">Homebrew</a>）</summary>

```bash
# agt：预编译的单个程序，装到 ~/.local/bin；重跑即升级
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/blacsheep/agt/releases/latest/download/agt-cli-installer.sh | sh
brew install python uv ffmpeg
brew install --cask font-noto-sans-cjk

# 可选：Google Chrome
brew install --cask google-chrome
```

</details>

字体或 Chrome 不在默认位置时，用 `WW_FONT`、`WW_FONT_BOLD` 指向字体文件，用 `CHROME` 指向浏览器程序。

## 快速开始

```bash
git clone https://github.com/blacsheep/werewolf-game && cd werewolf-game
export AGT_HOME="$PWD/.agt"          # 或者用 direnv 加载 .envrc

cp models.env.example models.env     # 填一个或多个厂商的地址、key 和模型名
scripts/setup-models.sh              # 每组填好的变量生成一个 agt 模型
scripts/setup-team.sh claude         # 传入上一步生成的模型名
scripts/console.sh &                 # 启动 Web 控制台，地址写在 recording/console.log
```

模型名是 `models.env` 里变量前缀的小写：`CLAUDE_*` 生成 `claude`，自己加的 `LUNA_*` 生成 `luna`，以 `setup-models.sh` 打印的 `ok <模型名> -> <模型>` 为准。传给 `setup-team.sh` 的模型供 lead、voice 和 replay 使用，也是上帝和玩家的默认模型。`models.env` 和 `.agt/` 都不进仓库，key 只保存在本机的 agt 里。

打开控制台，私聊 lead，说「开一局狼人杀」。lead 会用一条消息确认以下几项，回复「默认」即开跑：

- **模型**：默认上帝和 12 名玩家都用 lead 的模型，也可以逐座指定。
- **板子**：目前只有 12 人预女猎守，规则见 [docs/rules.md](docs/rules.md)。
- **产出**：默认语音、回放和视频都要。
- **其他要求**：比如玩家风格、解说语气。

对局编号由引擎自动分配（`games/` 下最小的未用编号，g001、g002……）。跑完后 lead 会汇报胜方、全员身份、产出路径和语音核验结果。再来一局，说同一句话即可。

> [!WARNING]
> `setup-team.sh` 默认把工具审批设为**自动放行**（`permissions.mode: auto_allow`）：一局要调用几百次引擎工具，逐次审批会让对局停住。其中 voice 和 replay 会以你的用户身份在本机执行命令，不在沙箱里，也不会先问你。请只在信任所用模型的环境里运行，或者放进虚拟机、容器。

不想全部放行的话，在 Web 控制台「运行设置」→「工具权限审批」里修改「Native 权限审核策略」：

- **模型审核，异常转人工**（`model_review`）：审核模型逐次批准，拿不准的转给你。审核模型已设为你传给 `setup-team.sh` 的模型，切换即可用，代价是每次工具调用多一次模型调用。
- **人工审批**（`manual`）：每次调用都等你批准，需要全程值守；超过 1 小时未处理的请求会取消这次执行。

重新运行 `setup-team.sh` 会恢复自动放行。详见 agt 文档：[权限审批](https://blacsheep.github.io/agt/zh/guide/permissions)、[本机命令执行](https://blacsheep.github.io/agt/zh/guide/local-execution)。

## 产出

| 文件 | 内容 |
| --- | --- |
| `games/<局>/events.jsonl` | 引擎事件日志（哈希链），对局的唯一事实来源 |
| `games/<局>/seats.json` | 每个座位的成员和模型 |
| `games/<局>/transcript.json` | 全部发言：公共群、狼队群、私聊、旁白 |
| `games/<局>/audio/full.mp3` | 整局语音 |
| `games/<局>/timeline.json` | 每句话在音频里的起止时间 |
| `games/<局>/replay/index.html` | 网页回放，用浏览器直接打开 |
| `games/<局>/replay/god.mp4` | 上帝视角视频 |

### 示例对局

[examples/sample-game](examples/sample-game) 是一局完整对局（狼人第 4 天获胜）的日志、座位表、发言和时间线。不配模型也能看它的网页回放（无声，字幕按原时间轴走）：

```bash
scripts/sample-replay.sh    # 生成 games/sample/replay/index.html
```

### 手动重做语音和回放

两个工具默认处理 `games/current`（最新一局），用 `--game` 指定其他局。公共群 ID 只在一局首次配音时传，之后从该局的 `tts.json` 读取；狼队群由引擎记录，不用传。

```bash
tools/tts/run.sh --game games/g001 [--public <公共群 ID>]
uv run --with pillow python tools/replay/build.py --game games/g001
```

细节见 [engine/README.md](engine/README.md)、[tools/tts/README.md](tools/tts/README.md) 和 [tools/replay/README.md](tools/replay/README.md)。

## 开发

```text
engine/              规则引擎：一个 MCP 服务器，每个座位一个锁定实例
prompts/             上帝、玩家、voice 和 replay 的提示词
docs/rules.md        板子和规则，引擎和所有 agent 都以它为准
docs/playbook.md     玩家打法资料
docs/skills/         lead 用的 werewolf-game，玩家用的 ww-playbook
docs/brief.md        从零搭建本项目时分阶段发给 lead 的需求（项目代码也由 agent 写成）
tools/tts/           配音：整理发言、edge-tts 合成、拼接、核验
tools/replay/        回放：网页、公开音轨、上帝视角 mp4
scripts/             初始化、控制台、示例回放、录屏
examples/            示例对局
```

测试不联网，也不调用模型：

```bash
python3 -m unittest discover -s engine/tests
uv run --no-project --with numpy python3 -m unittest discover -s tools/tts/tests
cd tools/replay && uv run --no-project --with pillow python -m unittest test_replay
```

回放测试需要一局带整局音频的对局，默认用 `games/current`，没有就跳过；可用 `WW_REPLAY_GAME` 指定，比如 `scripts/sample-replay.sh` 生成的 `games/sample`。

<details>
<summary><b>录屏</b></summary>

```bash
uv run --with playwright python scripts/record.py --name run1   # 每 3 秒截一张控制台
echo <会话 ID> > recording/focus                                  # 切换到某个会话
touch recording/stop                                               # 停止
scripts/make-video.sh run1                                         # 合成加速的 mp4 和 webp
```

</details>

## 已知限制

- 只有一个板子，换板子要改引擎、规则和提示词。
- edge-tts 用的是微软的在线朗读服务，不是公开 API，可能限流或变更；普通话音色只有 11 个，部分座位只能靠音调和语速区分。
- 模型偶尔不守发言规则，比如超长或私下拉群。引擎会拒绝非法动作，但管不了模型说什么。
- 一局的模型调用量很大，开跑前请先估算费用。

## 致谢

感谢 [LINUX DO](https://linux.do) 社区的支持与交流。

## License

[MIT](LICENSE)
