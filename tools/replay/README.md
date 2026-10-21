# 狼人杀回放

在仓库根目录一条命令生成网页、公开音轨和上帝视角视频：

```bash
UV_CACHE_DIR=/tmp/replay-uv-cache uv run --with pillow python tools/replay/build.py
```

指定其他完整对局：`UV_CACHE_DIR=/tmp/replay-uv-cache uv run --with pillow python tools/replay/build.py --game games/g002`。
仅生成网页与公开音轨：增加 `--web-only`；这不代表视频验收完成。

要求：Linux/POSIX、Python 3.10+、uv 和 Pillow（本轮 13.0.0）、本仓库 `engine/server.py`、ffmpeg/ffprobe（libx264、libmp3lame、AAC）、Google Chrome 或 Chromium，以及 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` 中文字体。uv 首次运行需要下载 Pillow；已有 Pillow 的环境可直接使用 python3。无外部头像。浏览器通过 DevTools pipe 控制，无调试端口；仅用于可信本地生成页面，使用 `--no-sandbox`，若执行环境禁止 Chrome 启动，须获得环境权限后重试。

输入只读：`timeline.json`、`events.jsonl`、`seats.json`、`audio/full.mp3`，以及 TTS 流程已有的 `transcript.json`（用于事件序号与播出公告映射）。事件日志经现有引擎校验哈希链并重放，确定身份、存活及警长；状态在对应公告播出时更新。字幕、句序和时间取自 timeline；胜负确定后的阶段统一标作“游戏结束”。模型、厂商仅取 seats 的明确字段或明确的全席配置；缺失显示未知。当前支持本仓库已结束、从零开始连续无重叠的 TTS 时间线；缺少公告映射或输入不一致会报错。游戏目录/日志权限须符合引擎要求。

输出位于 `<game>/replay/`：

- `index.html`：12 席圆桌、字幕、阶段、发言高亮、出局灰化、警徽、视角与进度/速度控制。直接用 Chrome 打开，保留相对路径。
- `public.mp3`：只含 public 区间，玩家视角使用它；上帝视角引用原始 `../audio/full.mp3`。
- `god.mp4`：1280×720 上帝视角，每句一张 Pillow 静态画面（与网页一致的圆桌布局及状态；字体排版不保证逐像素一致），以 VFR 按时间轴拼合，不逐帧录屏；结尾加一张保持帧补齐音频封装时长。
- `frames.json`：每页字幕一帧：句子编号、页码、时段、字幕/图片 SHA-256、说话人、实际绘制座位状态和字幕分行映射。
- `build-report.json`：输入哈希、公开音轨区间/时长、编码命令、MP4 大小、时长差、逐帧 PTS 及解码画面像素比对验证。

默认从玩家视角开始，跳过 wolves/private 内容；切换时映射到同一句的对应时间，若该句私密则跳至下一句公开内容，字幕/阶段/高亮同步更新。终局之前隐藏真实身份；终局根据公开结局揭晓身份和 seats 中的模型信息。上帝视角始终显示真实身份。玩家发言中自称或推断的身份属于公开原文，会原样保留。

**隔离边界**：这是本机静态回放，不是访问控制系统。HTML 的源数据包含完整字幕及身份，有文件权限的人仍可查看源码、原音频或主动切到上帝视角；不能将该目录当作可安全交付给未获授权玩家的数据包。玩家模式保证正常可见画面与所播音轨仅包含公开信息，终局身份揭晓除外。

构建先检查至少 700MB 可用空间和 512MB 估计可用内存（结合宿主机、cgroup 限额及可回收 inactive_file 缓存）；Chrome 验收前至少 700MB。ffmpeg 编码使用 1280×720、`-threads 1 -x264-params rc-lookahead=10 -tune stillimage`，解码和滤镜同样限制线程。不要并行运行重任务。公开音轨顺序读取 PCM，内存不随分句数增加。临时 PCM、Pillow 帧及编码文件置于 `tools/replay/.build-*`，本轮成功/普通异常后自动清理；机器宕机可能留下临时目录。产物全部通过检查后替换同名回放文件，不改原始输入；不要并行构建同一对局。旧任务遗留目录不会自动清理。较长或高采样率对局可能需要更多空间。

验证命令：

```bash
UV_CACHE_DIR=/tmp/replay-uv-cache uv run --with pillow python -m unittest discover -s tools/replay -p 'test_*.py' -v
python3 tools/replay/verify_browser.py
```

按上面顺序先跑单元测试，再串行运行构建命令，最后运行第二条 Chrome 验收命令。第二条仅启动一次 Chrome，检查警上、狼队夜聊、出局、终局在两种视角中的 DOM，逐条核验所有 wolves/private 句子的上帝展示及切换玩家视角后跳至公开内容，并实际点击视角/上一句/下一句/进度控件；只保存 `election-player.png`、`wolves-god.png`、`eliminated-player.png`、`final-player.png` 四图及 `browser-report.json`，随即关闭浏览器。HTML 使用 `preload="none"`；验收额外通过 DevTools 阻止音轨请求，并断言音频 readyState=0、buffered=0，避免加载整局音频。因此本轮浏览器验收不包含真实播放或试听。已有旧截图不会删除，只有 browser-report 中列出的四图属于当前验收。第一条在视频尚未生成时会明确跳过帧映射测试，生成后须重新运行。

画面设计：夜晚深蓝星空、白天暖色；座位按阵营着色（狼人红、神职金、平民蓝），当前发言人放大发光，中央显示发言人、身份和频道，顶部显示各阵营存活人数。长发言按标点切成最多两行的字幕页，按字数比例分配到这句的朗读时长（`render.cues`），网页回放则整句显示并随音频自动滚动。

MP4 检查要求视频/音频各自相对 full.mp3 封装时长差不超过 0.1 秒，文件小于 300,000,000 字节；逐页帧 PTS 误差不超过 1.1ms（视频时间基 1/1000 秒）。MP3 编码延迟/尾部填充、AAC 帧和容器时间基会产生毫秒差异；不能把编码后的包时长当作无限精度采样时间。最后画面保持至音频末端，源音轨缺少的封装尾部用静音补齐。

编码采用 CRF 24，不设置 VBV maxrate/bufsize：稀疏 VFR 配合 1/1000 时间基时，过低 VBV 可留下上一句字幕块。构建会顺序解码全部字幕页帧及结尾保持帧，分别比较整图和字幕区域与原始 Pillow 帧的像素平均绝对误差，阈值为 8 位像素的 3/255；失败时不发布。视频大小仍在发布前严格检查 <300MB。
