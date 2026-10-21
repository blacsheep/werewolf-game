# tools/tts：整局文字整编与语音回放

把一局的引擎事件日志和两个群聊合并成 `transcript.json`，用 edge-tts 逐句配音，拼成整局音频和时间线，最后自动核验。

## 一条命令

```bash
# 在仓库根目录
tools/tts/run.sh                                   # 处理 games/current，群 ID 读自 games/current/tts.json
```

新一局第一次运行时传入群 ID，它们会写进 `games/<局>/tts.json`，之后不用再传。狼队群可以不传：上帝在对局中用引擎记下的狼队群（`games/<局>/conversations.json`）会自动使用。

```bash
tools/tts/run.sh --public <公共群ID> --wolves <狼队群ID>
tools/tts/run.sh --public <公共群ID> --wolves <狼群ID1> <狼群ID2> <狼群ID3>   # 狼人发言分散在几个群时全部列出
```

多个狼群时 `tts.json` 的 `wolves` 存成列表，每个群单独拉取（`.tts/chat-wolves-<群ID>.json`），任一群读取失败即报错退出、不会跳过。各群消息的频道都是 `wolves`，但 `source.chat`（`wolves@<群ID>`）、`source.conversation_id` 和 `seq` 保留它实际所在的群；`exclude`/`include` 的键相应写成 `wolves@<群ID>:<seq>`。核验会检查 `tts.json` 里的每个群都已读入、每条群消息的来源群一致。

常用参数（原样传给 `pipeline.py`）：

| 参数 | 作用 |
| --- | --- |
| `--game games/g002` | 处理指定对局目录（默认 `games/current`） |
| `--offline` | 不联网拉群聊，使用 `<局>/.tts/chat-*.json` 中上次拉取的原始结果 |
| `--transcript-only` | 只生成 `transcript.json` |
| `--asr [--asr-model-dir DIR]` | 抽检时再用 faster-whisper 识别语音并与文本比对（首次会下载约 460MB 模型） |
| `--samples 8 --seed N` | 抽检句数和随机种子（默认随机，种子写入核验报告） |
| `--no-verify` | 跳过核验；单独核验：`uv run --no-project --with edge-tts==7.3.1 --with numpy python3 tools/tts/verify.py [--asr]` |

依赖：`uv`（自动拉取 `edge-tts==7.3.1`、`numpy`）、`ffmpeg`/`ffprobe`、可用的 `agt` CLI（以 `web-operator` 读群历史），合成时需要能访问微软 edge-tts 服务。群聊拉取失败会直接报错退出，不会用任何替代内容。

## 输入与输出

输入（只读）：`<局>/events.jsonl`、`<局>/seats.json`、两个群的历史（`agt tool call --principal web-operator message_history`，按 `next_cursor` 传 `before` 翻页直到取完）。

| 输出 | 内容 |
| --- | --- |
| `<局>/tts.json` | 本局配置：群 ID；可选 `exclude`（`{"public:77": "原因"}`）和 `include`（`["wolves:9"]`）手工覆盖过滤结果 |
| `<局>/.tts/chat-public.json`、`chat-wolves.json` | 群聊原始结果（0600，仅本地生成用，不含凭据） |
| `<局>/.tts/cache/` | edge-tts 原始音频缓存（按音色参数+文本哈希），重跑只合成变化的句子 |
| `<局>/transcript.json` | 每句：`index`、`id`、`speaker`、`speaker_name`、`channel`、`day`、`phase`、`phase_label`、`kind`、`is_narration`、`actor`、`text`、`tts_text`、`at`、`source`；另含 `sources`、`rules`、`excluded`（每条排除的群消息及原因）、`event_coverage`（每个事件对应的句子或不录入原因） |
| `<局>/audio/L0001-<说话人>.mp3` … | 分句音频（含句后停顿），与 transcript 的 `id` 一一对应 |
| `<局>/audio/full.mp3` | 整局音频 |
| `<局>/timeline.json` | 每句 `start`/`end`（秒，含停顿，相邻条目首尾相接）、`voice_end`、样本区间、说话人、频道、阶段、文本、`audio`、实际使用的音色参数 |
| `<局>/tts_verify.json` | 核验报告 |

重跑时 `audio/` 下只会删除本工具命名格式（`L四位数字-说话人.mp3`）且本次不再使用的旧分句文件，其他文件不动。

## 整编规则

- 主线是事件日志：玩家发言、遗言取 `speak`/`last_words` 原文。群里与事件逐字相同的玩家消息算重复，不再录入。
- 引擎事件转成旁白，`speaker` 为 `narrator`、`is_narration: true`，用单独的旁白音色，不算任何人说过的话：
  - 守卫、女巫、预言家的操作 → `private:pXX`（上帝与该玩家的私密频道）；狼刀 → `wolves`；
  - 警长报名/退水（连续事件合并成一句）、警长定序、移交警徽、开枪、自爆，以及引擎公告（入夜、死讯、票型、放逐、胜负）→ `public`。
  - 投票保密，不逐张念；引擎公布票型时合成一句公开旁白，`source.ballot_seqs` 列出对应的投票事件。
  - 个人笔记（`note`）不录入。
- 群聊纳入上帝主持词（公共群、狼队群）和狼人夜间讨论。排除：玩家的“已投票”；含主持阻塞、工作汇报、`next_step`/`mapping_required`/`export_replay`/`game_over`、“被引擎拒绝”、“只有/仅剩 note”等工具状态内容的消息；已撤回消息；非主持/玩家发送者。规则在 `transcript.py` 的 `classify_chat`，个别消息可用 `tts.json` 覆盖。
- 清理：`pXX`、`@pXX`、阿拉伯数字座号（如“11号”）一律改成“十一号”这样的写法；去掉 Markdown 与 @；多行合并成一段。`tts_text` 只把下划线、括号换成空格方便朗读，`text` 保持清理后的原文。
- 排序：事件用 `at`，群消息用 `created_at`，统一换算成时间戳排序；同一时刻先事件后消息，再按序号稳定排序。阶段由引擎 `engine/server.py` 的 `Game` 重放日志得到，群消息取发送时刻所在的阶段。

## 音色

`voices.json` 固定了上帝、旁白和 12 个座位的音色，每局沿用。edge-tts 的普通话音色只有 11 个（zh-CN 6 个、zh-TW 3 个、东北话和陕西话各 1 个；zh-HK 是粤语，不用），所以 10、11、12 号复用 1、2、3 号的音色，用更低的音调和更慢的语速区分。改音色只需编辑这个文件，重跑时会重新合成受影响的句子。句间停顿和码率在 `audio` 段。

## 核验（verify.py）

- transcript：序号与 id、必填字段、频道合法、无 `pXX`/Markdown/“已投票”/非对局内容、旁白标记、私密频道、每条发言都已录入、每个事件都有覆盖、时间排序。
- timeline：与 transcript 一一对应；从 0 开始首尾相接、无重叠；每条都有实际存在的音频；`ffprobe` 检查每个文件的编码、采样率、声道和时长（与时间线偏差 ≤ 0.08 秒），full.mp3 时长与时间线总长一致。
- 说话人：按 `voices.json` 为每个说话人另外合成 3 句固定的注册参考句（缓存在 `<局>/.tts/voice-ref/`），提取全部分句的倒谱均值/方差与基频，以“该说话人的其他分句 + 注册参考句”为质心做最近质心判别，要求每句都判给它标注的说话人（只说过一句话的座位靠注册参考句覆盖）。
- 随机抽检（默认 8 句，每次至少一条旁白、一条主持词、一条狼人讨论）：
  - 从 full.mp3 按时间线截取片段，与分句文件做能量包络相关（r ≥ 0.9、偏移 ≤ 30ms），确认时间线指向的就是这句；
  - 绕过缓存重新请求 edge-tts，用标注音色和两个最易混的音色（同底音色的变体优先，其次基频最接近）各合成同一整句，分句文件的对数梅尔频谱图必须与标注音色 r ≥ 0.95，并比对照音色高出 0.05 以上；
  - `--asr` 时用 faster-whisper 识别分句文件，与 transcript 文本的字符相似度须 ≥ 0.75。
- 这些都是自动检查，不等于人工试听。

## 测试

```bash
python3 -m unittest discover -s tools/tts/tests -v
```

测试不联网、不需要 edge-tts：文本清理、过滤规则、用真实引擎驱动的一小局整编、假 `agt` 的翻页与报错、音频拼接的连续性和清理范围；频谱相似度测试需要 numpy（可用 `uv run --no-project --with numpy python3 -m unittest discover -s tools/tts/tests`）。
