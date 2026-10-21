# 发给 lead 的需求

日常开局不需要这份文件：lead 的 skill werewolf-game 已经包含整个流程，对 lead 说「开一局狼人杀」即可。这里保留从零搭建项目时的原始需求，整个项目由人发给 lead 四条消息驱动，每个阶段一条，按顺序发送。除了 `scripts/setup-*.sh` 准备的模型、两个编码 harness 和 lead 本身，其余的 agent、提示词、skill、MCP 服务器、群聊和代码都由 agent 在 agt 里完成。

项目目录：本仓库根目录。规则：`docs/rules.md`。打法资料：`docs/playbook.md`。

---

## 第一阶段：引擎

> 我们要在 agt 里办一场 AI 狼人杀，12 人预女猎守，规则在 docs/rules.md。第一步请做规则引擎：
>
> 1. 用 codex harness 创建一个 Codex agent `toolsmith`，把一份精确的规格作为任务交给它。它在 engine/ 目录下写一个 MCP 服务器（stdio，Python 标准库即可）并配套测试。
> 2. 引擎是唯一记录游戏状态的地方。状态放在 games/current/ 下（current 是指向当局目录的符号链接），每次状态变化追加写入 events.jsonl。上帝和玩家都不需要自己记状态。
> 3. 同一个脚本按身份锁定：环境变量 WW_SEAT=god 或 p01–p12。玩家进程只能看到自己的信息，狼人能看到队友，工具参数里没有任何办法查看别人的身份。夜里的技能、投票、遗言、开枪、自爆、个人笔记，全部通过引擎工具完成，并由引擎校验合法性。
> 4. 上帝的工具里要有一个 next_step()：根据规则告诉上帝下一步该做什么（叫谁发言、在哪个会话、开放哪些操作），上帝只需要照做。夜间结算、投票计票、胜负判定都由引擎完成。
> 5. 投票保密：玩家只能用工具投票，全部投完后上帝才能拿到票型。
> 6. 每局结束能导出完整事件日志（含所有夜间操作），之后的上帝视角回放要用。
> 7. 测试要覆盖规则里的边界：同守同救、守卫连守、女巫不能自救、猎人被毒不能开枪、警长竞选退水和 PK、自爆、平票 PK、屠边判胜。
>
> 引擎完成后，注册 13 个 MCP 服务器（ww-god、ww-p01 … ww-p12），每个都锁定到一个身份，逐个探测工具确认隔离生效，然后把结果告诉我。暂时先不要创建上帝和玩家。

## 第二阶段：组队并开局

> 引擎好了，现在组队：
>
> 1. 让 toolsmith 建好对局目录 games/g001，并把 games/current 指向它（你没有 shell，这一步交给它）。
> 2. 创建上帝 `god`，模型用 luna，只挂 ww-god，响应策略设为「所有消息」。它只负责主持：反复调用 next_step()，按指示用 message_send 点名（必须带 mentions），宣布死讯和票型。语气像专业比赛的主持人，简洁、不评论、不泄露信息。
>    - 夜里：开局后上帝自己读身份表，建狼队群（上帝和 4 名狼人）。神职的夜间行动在上帝和该玩家的私聊里完成，私聊用 direct_create(座位) 获取，同一对人永远是同一个私聊。
>    - 投票：在公共群里一次 @ 全部有投票权的人。玩家用 vote 工具投票，在群里只回「已投票」，不说投给谁；上帝等全部人回过之后再调用 advance 公布票型。
>    - 上帝每次行动后都要再调用 next_step()，直到对局结束；结束时宣布胜负和全部身份。
> 3. 创建 12 名玩家 p01–p12，在群里显示为「1号」到「12号」，模型都用 luna，每人只挂自己的 ww-pXX，响应策略是只在被点名时回复。座位和模型的对应关系写进 games/current/seats.json（让 toolsmith 写；以后换成多家厂商时，回放会用它揭晓每个座位的模型）。
> 4. 所有玩家用同一份提示词，挂同一个 skill。skill 根据 docs/playbook.md 来写，要达到京城大师赛的专业水准。发言要求：口语化，200–500 字，不要 Markdown、列表或表情，号码一律写成「几号」（之后要转成语音）。轮到发言时，先用 speak 工具提交发言，再把同样的文字作为回复发出来。各身份的打法（playbook 第三节）也写进这个 skill：所有人拿到的内容一样，不会泄露身份；自己是什么身份由 my_info() 告诉玩家。
> 5. 建公共大群「狼人杀 第1局」（我、上帝、12 名玩家），把群 ID 告诉上帝。
> 6. 一切就绪后让上帝开局，一直打到分出胜负。中途不需要问我，出了问题再找我。
>
> 完成标准：games/current/events.jsonl 里有 game_over 事件，上帝在公共群里宣布了胜负。在那之前不要停下来向我汇报进度；你创建完团队、上帝开局之后，就由上帝推进。

## 第三阶段：语音

> 游戏结束了。用 claude-code harness 创建 agent `voice`，通过任务交给它：
>
> 1. 合并出 games/current/transcript.json（顺序、说话人、所在频道、阶段、文本）：
>    - 引擎事件日志 games/current/events.jsonl 是主线：玩家发言、遗言、投票和夜间操作都在里面。
>    - 上帝的主持词在公共大群，狼人夜里的讨论在狼队群。voice 不是这两个群的成员，在本机用 agt 命令行以 web-operator 身份读取：`AGT_HOME=<仓库>/.agt agt --output json tool call --principal web-operator message_history --args '{"conversation":"<群 ID>","limit":200}'`（用 next_cursor 翻页）。群 ID 由我在任务里给你。
>    - 夜间神职操作（守卫守谁、女巫用药、预言家查验、狼刀）按事件日志写成上帝视角的旁白，例如「守卫守护了五号」，频道标为对应的私密频道。
>    - 清理文本：pXX 一律改成「几号」；去掉 Markdown；去掉不属于对局的消息（比如上帝最后发的汇报）；投票后的「已投票」不需要念。
> 2. 用 edge-tts 把每一句转成中文语音。上帝和 12 名玩家各用一个固定且容易区分的音色；中文音色不够 13 个，就用语速和音调区分。音色表写进 tools/tts/voices.json，以后每局沿用。
> 3. 输出 games/current/audio/ 下的分句音频、拼好的整局音频 games/current/audio/full.mp3，以及 games/current/timeline.json（每句在整局音频里的开始、结束、说话人、频道、文本、阶段）。
> 4. 代码放在 tools/tts/，以后每局只需运行一条命令。
>
> 完成标准：timeline.json 里每一条都有对应的音频，时间连续不重叠；随机抽几句播放确认是对的人在说对的话。

## 第四阶段：回放

> 用 codex harness 创建 agent `replay`，通过任务交给它：
>
> 1. 读取 timeline.json、音频、seats.json 和事件日志，生成 games/current/replay/index.html：12 个头像围成一圈，谁说话谁高亮，下方显示字幕；顶部显示阶段（第 1 夜 · 狼人行动、第 2 天 · 警上发言）；出局的人头像变灰；警长有标记。
> 2. 上帝视角：夜里狼队频道和神职操作也要播放，并标出各玩家的真实身份；可以切换成「玩家视角」，此时隐藏夜间私密内容和身份。
> 3. 游戏结束时揭晓每个座位的身份和模型厂商。
> 4. 再用 ffmpeg 导出一个上帝视角的 mp4，长度与整局音频一致。画面大部分时间是静止的：按 timeline 每一句渲染一张画面，再配上 full.mp3 合成，不要逐帧录屏。机器只有 3.6GB 内存，必须省内存：
>    - 逐句画面用 Pillow 画（`uv run --with pillow`，中文字体 /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc），布局与网页一致，不要用 Chrome 截图生成画面；
>    - ffmpeg 用 1280×720、`-threads 1`、`-x264-params rc-lookahead=10`，静态图拼接（concat）加 `-tune stillimage`；
>    - 重活一次只跑一个，不要并行；Chrome 只在最后验收截图时短暂启动一次，用完关闭，页面音频用 `preload="none"`；
>    - mp4 控制在 300MB 以内。
> 5. 代码放在 tools/replay/，以后每局只需运行一条命令。
>
> 输入说明：timeline.json 的 entries 每条有 start、end、speaker（god、narrator、p01…p12）、channel（public、wolves、private:pXX）、phase_label、kind 和 text；玩家视角只播放 channel 为 public 的条目。存活、警长、身份以事件日志为准。没有头像图片，用座位号加配色自己画。
>
> 完成标准：用无头 Chrome 打开 index.html，在几个不同时间点截图（警上发言、狼队夜聊、出局后、终局揭晓），确认高亮、字幕、阶段、灰化和身份显示都对；mp4 的时长与 full.mp3 一致。
