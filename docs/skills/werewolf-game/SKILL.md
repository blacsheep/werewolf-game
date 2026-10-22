---
name: werewolf-game
description: 用户要办一局 AI 狼人杀时，lead 先和用户对齐模型、板子和产出，然后自己跑完组队、主持、语音和回放视频的全流程。只给 lead 用。
---

# 办一局 AI 狼人杀

项目目录 {{root}}。规则在 docs/rules.md，玩家打法在 docs/playbook.md，各阶段的细节和验收标准在 docs/brief.md。用户说「开一局狼人杀」「再来一局」这类话时，按下面的步骤做。

## 第零步：对齐需求（整局唯一一次问用户）

先用 resource_list 看看现有的模型（kind=model）、agent、MCP 服务器和 skill，再用一条消息问用户下面几项。每项都给出默认值，并告诉用户：回复「默认」就直接开跑。

1. 对局编号：默认用 games/ 下一个没用过的编号（g001、g002……），编号交给编码 agent 去确认。
2. 模型：默认上帝和 12 名玩家都用你自己用的模型（用 resource_list 查 lead 这个 agent 的模型）。用户也可以给每个座位指定不同模型，但只能从已有的模型资源里选；想用还没配置的厂商，要请用户先填 models.env 并运行 scripts/setup-models.sh。key 由用户自己提供，你不要索要。
3. 板子和规则：默认 12 人预女猎守，规则见 docs/rules.md。引擎目前只支持这一个板子。换板子或改规则，要先让编码 agent 修改引擎并补测试，耗时较长，需要用户明确同意。
4. 产出：默认三样都做，分别是整局语音、网页回放（可切换上帝视角和玩家视角）、和整局音频等长的上帝视角 mp4。
5. 其他要求，比如玩家风格或解说语气。没有就跳过。

用户答复后，复述一遍最终配置，然后马上开工，不要再等用户确认。之后除非真被卡住，不要再问用户。只有工作空间所有者能回答的问题才用 goal_wait_human。

## 第一步：建 goal

goal_create 写明本局的配置。done_when 写成可以核验的条件：
- games/<局>/events.jsonl 里阶段已经到 finished，上帝在公共群宣布了胜负；
- 用户要的产出都已存在：audio/full.mp3、timeline.json、replay/index.html、replay/god.mp4；
- god.mp4 的时长和 full.mp3 一致。

## 第二步：准备对局目录和座位表

你没有 shell。文件操作交给编码 agent：优先复用 toolsmith（codex harness，提示词 toolsmith）；没有的话就创建一个。用 task_delegate 交给它：
1. 编号取 games/ 下（不含 games/archive）还没有的最小编号，新建 games/<局>，把 games/current 指向它（相对符号链接）。不要动其他对局的目录。
2. 写 games/<局>/seats.json：`{"seats": {"god": {"model": "...", "principal": "<局>-god"}, "p01": {"model": "...", "principal": "<局>-p01"}, ...}}`。引擎从 principal 拿到成员 ID：上帝点名用玩家的，玩家私聊表态用上帝的。回放结束时用 model 揭晓每个座位的模型。

## 第三步：组队

agent 和成员 ID 一律用 `<局>-god`、`<局>-p01` … `<局>-p12` 这样的形式，每局新建，不复用上一局的玩家，也不要删除旧局的 agent。
- 上帝 `<局>-god`：使用第零步定下的模型，提示词 ww-god，只挂 MCP ww-god。
- 玩家 `<局>-pXX`：使用该座位定下的模型，提示词 ww-player，skill ww-playbook，只挂自己的 ww-pXX。显示名为「1号」…「12号」，响应策略保持「只在被点名时回复」。
- 每个 agent 建好后都要创建成员身份并绑定（principal_create，然后 agent_bind）。

如果 ww-god、ww-p01 … ww-p12 这些 MCP 不存在，说明引擎还没装好。这时先按 docs/brief.md 第一阶段做完引擎，再回来继续。

## 第四步：建群、配置引擎、开局

1. 建公共群「狼人杀 第N局」，成员是你、上帝和 12 名玩家。工作空间所有者会自动加入每个群和私聊，身份是观众。
2. 更新 MCP ww-god 的环境变量 WW_CONVERSATIONS 为 `{"public": "<公共群 ID>"}`，保留 WW_SEAT=god 和其他字段。狼队群和私聊由上帝自己建，不要预先填。上帝点名用的成员 ID 由引擎从 seats.json 的 principal 读取，所以第二步的座位表必须写对。
3. 在公共群里点名上帝，宣布开局。之后由上帝主持到终局，你不用一步步盯着。只有上帝在你们的私聊里报告异常时，才需要你介入。

上帝或玩家停住时（包括收到 goal 审核时）：先看引擎状态和最近的消息，然后直接在公共群点名该行动的人（通常是上帝），只核对不点名不算疏通，也不要因此向用户汇报。不要替玩家做决定，不要替上帝念主持词，也不要直接改游戏状态。模型网关偶尔会慢十几分钟，超时后会自动重试，这种情况不算卡住。

## 第五步：语音和回放

确认终局后（上帝宣布了胜负，引擎阶段是 finished）：
1. 语音：复用 voice agent（claude-code harness，提示词 voice），没有就创建。用任务交给它：在项目目录运行 `tools/tts/run.sh --public <公共群 ID>`。狼队群由上帝在对局中记在 games/<局>/conversations.json，工具会自动读取。它会生成 transcript.json、audio/full.mp3、timeline.json 和 tts_verify.json。核验报告里有问题的话，让它说明是哪几句、为什么。
2. 回放：语音完成后，复用 replay agent（claude-code harness，提示词 replay），没有就创建。用任务交给它：运行 `UV_CACHE_DIR=/tmp/replay-uv-cache uv run --with pillow python tools/replay/build.py`，生成 replay/index.html 和 replay/god.mp4，并用 ffprobe 核对 mp4 和 full.mp3 的时长。
3. 回放要用语音的产物：语音做完再做回放，不要同时开两个任务。

用户不要某项产出时，跳过对应的步骤。

## 第六步：收尾

逐条对照 done_when 核验，依据要写成能复查的事实，比如阶段 finished、胜方、文件路径和两个时长。核验完成后 goal_complete，再向用户汇报：胜方和全员身份、产出路径、语音核验结果、本局出现过的异常。没有全部完成前，不要中途向用户汇报进度：处理 goal 审核、任务结果或核对时，你在和用户私聊里的最终回复写 [[silent]]，只有整局完成的汇报和真需要用户回答的问题才写出来。
