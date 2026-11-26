# 帧川 Zhenchuan

小说进，漫剧出。上传一本短篇，帧川按镜头把竖屏 AI 漫剧做完：编剧、角色定妆、分镜、预演、视频、配音、口型、字幕和成片。镜头是工作单位。上游一就绪，这个镜头就往下走，不必等整集。

默认画幅是 2D 动漫、9:16、720×1280。不配置任何密钥时，全流程用本地模拟供应商跑完，并产出带字幕和「AI生成」标识的 mp4。

A novel goes in, a vertical AI short comes out. The unit of work is the shot. As soon as one shot's upstream artifact exists, that shot moves on. The default picture is 2D anime, 9:16, 720×1280. With no API keys, mock providers render the whole film locally.

## 在页面里改三条接口

打开制作台的「模型设置」。三条主接口是：

1. **大脑 / 编剧**：总控和编剧共用。供应商选「OpenAI 兼容 / DeepSeek / 中转」，Base URL 填 `https://api.deepseek.com/v1`（自建中转就填中转地址），模型填 `deepseek-chat` 或 `deepseek-reasoner`，把 API Key 贴进去。密钥只存在数据库里，页面之后只显示掩码。
2. **文生图**：Seedream，或兼容的图片接口。
3. **视频**：Seedance、可灵，或通用 HTTP 轮询。

点「测试连接」会打一次最小请求并显示耗时和结果。保存后下一次调用立即生效，不用重启。环境变量仍然是空密钥时的后备；数据库里的值优先。选「本地模拟」可以不填密钥跑完整部片子。配音、口型和配乐收在折叠区，默认本地模拟。

每个项目的「Agent 协作」页能看到总控派给编剧、定妆、分镜、视频、配音、质检的消息，以及每一次接口调用（供应商、模型、耗时、费用、错误）。

## 长篇、资产和样片

制作板和项目列表会显示总进度、当前在做什么（例如「分析章节 37/120」）、最近一次活动是几秒前、上一次调用的模型，以及按最近耗时估的剩余时间。超过大约 3 分钟没有新调用会标成「疑似卡住」。失败会写成「出错」，并给出错误和「重试」，不会一直停在「制作中」。老项目如果是升级前开的，没有计数时进度条会空转，不会假装是 0%。

章节分析默认按约 2.4 万字一块，并且最多 12 路同时请求（模型设置里可改到 16）。分析默认走 `deepseek-chat`，即使编剧模型选了 reasoner；也可以单独填一个「分析用快速模型」。已完成的章节笔记会按正文哈希记下，重试不会再打同一段。默认打开快速开拍：只精读试做那几集要用的章节，再用每章开头和结尾做临时圣经。短章节会合并成约 2.4 万字一块。集数在开拍前选（20/40/60/80 或按章节自动建议，最多 100），也可以只改编第几章到第几章。试做做完停在「试做完成」，再选继续下几集、全部继续或停止。全书补读默认关闭。进度行会写「快速开拍」和每分钟调用次数。

工人重启后，心跳超过 `STALL_SECONDS`（默认 180 秒）的任务会变成「已中断」。点「继续」会重跑这一步，不会再出现空转成功、项目却一直停在「制作中」。

项目页、列表和进度按 SQL 计数，不把全部任务读进内存。API 启动时，集行数超过已保存计划的项目会收成该计划：试做那几集的剧本和章节笔记留下，其余空大纲和排队任务删掉。删掉的任务即使被重新投递，也不会重开全书补读。

环境变量 `CHAPTER_CONCURRENCY`、`CHAPTER_CHARS`、`STALL_SECONDS` 可以改这三件事。

小说不再在 1.2 万字处截断。有章节标题时按章切开，先做整本故事圣经（人物在不同阶段的外形、世界、时间线），再列分集大纲。默认计划 60 集、每集 6 个镜头，先写出前 2 集；后面的集停在大纲上。制作板的「故事圣经」里可以点「再写下一集」。下一集的编剧不依赖上一集的成片，所以可以一边渲染一边写。

「模型设置」顶部有「一键配置中转站」：填一个 Base URL 和密钥，大脑、文生图、视频都写成 OpenAI 兼容。一个槽可以存多把密钥，一行一把，按轮询使用；某一把返回 429 或 5xx 会冷却并换下一把。页面仍然只显示掩码。

「资产」页收角色、场景、道具和服装。角色可以出三视图，场景可以出四视图。改一张卡会把用到它的镜头标成待更新，并可以一次重做。卡片能放进跨项目库，再导入别的项目。每次修改留一个版本，可以退回。资产库的组织方式参考了 Jellyfish（Apache-2.0），代码是帧川自己的。

「剧本」页可以直接改画面和对白。选中一段后按要求让模型改写，红字是删掉的，绿字是加上的，可以按段接受或放弃。改动会标出受影响的后续镜头。

视频默认按 Seedance 的多参考来组请求（角色、场景、道具），超出该接口上限的参考图会被截掉。支持首帧加尾帧。连续镜头可以合并成一条 15 到 30 秒、带时间戳的生成。制作板上的「分镜格」把关键帧拼成格子，确认后再出视频（需要项目打开分镜确认时才会拦住）。可灵和通用 HTTP 接口会按各自能力降级，不会假装支持同样的参考张数。

开拍前的报价同时给出低清样片和成片两个数。项目打开样片模式后，镜头先以约 480p 生成；镜头或整集可以再点一次做成成片。上游任务超时后进入「观察中」，多次复查后变为「待核实」，查的是原来的任务号，不会再扣一次。积分页（管理员钥匙 `zc-dev-admin`）列出这些任务；最终失败会退回这一笔。

如果 API 被映射到宿主机的 8010（`docker-compose.override.yml` 里常见），构建前端时加上 `NEXT_PUBLIC_API_URL=http://localhost:8010`。容器内部端口仍然是 8000，服务名不变。

## 快速开始

需要 Docker。

```bash
docker compose up --build
```

- 制作台：<http://localhost:3000>
- API：<http://localhost:8000/api/health>
- 对象存储控制台：<http://localhost:9001>（`minio` / `minio12345`）。开发环境用 Silo，接口与 MinIO 相同；官方 `minio/minio` 镜像已不能匿名拉取。

打开制作台，点「用《雨夜来信》开拍」。示例小说在 `samples/short_novel.txt`。进度板会按镜头显示分镜、预演、视频、配音、口型、质检。成片在「成片与费用」。

本地只跑测试，不需要 Docker：

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest
```

机器上需要 `ffmpeg` 和一种中文字体（Debian/Ubuntu：`fonts-wqy-microhei`）。

## Where the three APIs are

Open **模型设置** in the nav. The brain slot is an OpenAI-compatible chat endpoint, defaulting to DeepSeek at `https://api.deepseek.com/v1` with model `deepseek-chat` (or `deepseek-reasoner`, or your relay). Image and video are the other two primary slots. Keys are stored in the database, masked in the UI, and applied on the next call without a restart. A saved key overrides the environment variable. Mock remains a provider you can select. **测试连接** makes one minimal call and shows latency or the error.

**Agent 协作** on a project shows the director dispatching 编剧, 定妆, 分镜, 视频, 配音 and 质检, plus the live API call log.

Host port 8010 via `docker-compose.override.yml` still works: the container listens on 8000 and the service name is `api`. Rebuild the web image with `NEXT_PUBLIC_API_URL=http://localhost:8010` if the browser should call that host port. Redis can stay unpublished.

Project and list pages count jobs in SQL. On API startup, a project with more episode rows than its saved plan is collapsed: the trial scripts and chapter notes stay, and the extra outline jobs are deleted so a restarted worker cannot resume a full-book fill.

## Setup

```bash
docker compose up --build
```

Open <http://localhost:3000> and use the bundled sample, `samples/short_novel.txt`. The progress board updates per shot. The finished mp4, burned-in subtitles, the AI label, and the cost report are on the watch page. Object storage is Silo (`pgsty/silo`), an S3-compatible stand-in, because the official MinIO images are no longer anonymously pullable. The console is <http://localhost:9001> (`minio` / `minio12345`).

```bash
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pytest
```

## 密钥

不填密钥就是模拟供应商。要换真模型，把 `.env.example` 抄成 `.env`，或直接把变量交给 compose。然后在开拍时选「国内商用」或「海外」。某个供应商缺密钥或调用失败时，默认会落到模拟供应商，流水线仍能出片。项目设置里的 `fallback_to_mock: false` 可以关掉这个行为。

| 变量 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容的对话和图片。DeepSeek 或自建中转把 base URL 指过去，路径仍是 `/chat/completions` 和 `/images/generations`。 |
| `ARK_API_KEY` | 火山方舟。图片 `POST /images/generations`（Seedream），视频 `POST /contents/generations/tasks`（Seedance）。 |
| `SEEDREAM_MODEL` | 默认 `doubao-seedream-4-5-251128`。4.0（`doubao-seedream-4-0-250828`）才接受 720×1280 像素；更新的模型会要 2K 的 9:16（1600×2848），流水线再缩回项目尺寸。 |
| `SEEDANCE_MODEL` | 默认 `doubao-seedance-2-0-260128`。首帧任务使用 `role=first_frame` 且 `ratio=adaptive`。2.5 的 id 是 `doubao-seedance-2-5-260628`，方舟对开通有余额要求。 |
| `KLING_API_KEY` | 可灵。图生视频 `POST /v1/videos/image2video`，口型 `POST /v1/videos/lip-sync`。只有旧版 AK/SK 时填 `KLING_ACCESS_KEY` 和 `KLING_SECRET_KEY`，适配器会签 30 分钟 HS256 JWT。新的 path 模型不接受 AK/SK。 |
| `MINIMAX_API_KEY` | `POST /v1/t2a_v2`，默认主机 `https://api.minimaxi.com`。 |
| `ELEVENLABS_API_KEY` | `POST /v1/text-to-speech/{voice_id}`，默认 `eleven_multilingual_v2`。 |
| `S3_PUBLIC_ENDPOINT` | 可灵口型要能下载到视频。本地 MinIO 通常做不到，模拟口型不需要。 |

价格表在 `backend/app/adapters/pricing.py`，是估算用的美元单价，不是账单。

## Keys

Leave every key empty and the mock providers run the film. Copy `.env.example` to `.env` for real vendors. If a call fails or a key is missing, the project falls back to mock unless `fallback_to_mock` is false.

`OPENAI_BASE_URL` is how a relay or DeepSeek is attached. Ark uses `ARK_API_KEY`. Kling prefers `KLING_API_KEY`; the access-key/secret pair is only for the legacy JWT. Kling lip-sync needs a video URL the vendor can fetch (`S3_PUBLIC_ENDPOINT`). MiniMax and ElevenLabs are the speech providers. Unit prices live in `backend/app/adapters/pricing.py` and are estimates.

## 架构

```text
小说 → 读入/分块 → 编剧 → 角色定妆 ─┐
                         场景概念图  │
                                    ├─ 锁定定妆（硬门槛）→ 每个镜头的分镜 → 视频 ─┐
                                    │                              ↘ 预演      │
                         临时配音 / 成片配音 / 配乐（不等定妆）──────────────→ 口型 → 质检
                                                                              ↓
                                                                     单集合成 → 成片+字幕+AI标识 → 终检
```

状态在 Postgres（测试里是 SQLite）。Celery 只负责把 job id 送去执行，worker 挂了可以按数据库里的状态重投。以后换 Temporal 时，保留这些表，把 `backend/app/pipeline/dispatch.py` 的 `enqueue` 换成 workflow 信号，阶段函数不用改。人工确认定妆是一个停在 `awaiting_approval` 的 job，通过之后才放行分镜。

质检看口型成片是否有画面、声音和时长。失败会再排一条「视频 → 口型 → 质检」，并把单集合成的依赖指到新的质检上。默认重试 1 次。

制作板用 SSE（Redis）推送，同时每 2 秒拉一次看板，所以丢事件也能跟上。

## Architecture

Jobs are rows. Celery transports ids; it does not own the graph. `dispatch.enqueue` is the seam for a later Temporal worker. Character lock is a real job: storyboard and video stay blocked until every character sheet is locked. Speech and music do not wait. QC can queue a replacement video chain for one shot while other shots keep moving.

## 加一个视频供应商

两条路。

配置即可，适合「创建任务、轮询、下载」的 HTTP 厂商。把项目的 video 路由设成 `http_poll`，`params` 例如：

```json
{
  "provider": "http_poll",
  "model": "vendor-x",
  "fallbacks": ["mock"],
  "params": {
    "create_url": "https://vendor.example/v1/jobs",
    "poll_url": "https://vendor.example/v1/jobs/{task_id}",
    "task_id_path": "data.task_id",
    "status_path": "data.status",
    "success_values": ["succeeded"],
    "failure_values": ["failed"],
    "video_url_path": "data.output.url",
    "auth_env": "VIDEO_HTTP_API_KEY",
    "body": {
      "model": "{model}",
      "prompt": "{prompt}",
      "image": "{image_data_uri}",
      "duration": "{duration}"
    }
  }
}
```

整个字符串刚好是 `{duration}` 时会保留整数类型。实现在 `backend/app/adapters/video/http_poll.py`。

要写代码时，新增一个类，实现 `image_to_video(...)`，在 `backend/app/adapters/registry.py` 的 `BUILDERS` 里登记 `("video", "my_vendor")`，并在 `pricing.py` 加上单价。阶段代码不用改。Seedance 和 Kling 是两个现成例子。

POST 创建任务只对连接错误和 HTTP 429 重试，避免 5xx 时重复扣费。轮询用的 GET 会重试 5xx。

## Adding a video provider

Use `http_poll` when the vendor is create-then-poll. The body template is in the JSON above; a placeholder that is the entire string keeps its Python type. For a real client, add a class with `image_to_video`, register it in `BUILDERS`, and add a price. POSTs retry connection errors and HTTP 429 only. Polling GETs also retry 5xx.

## 费用

每个模型调用（含失败）写入 `model_calls`。成片页有三列数字：

- 已计量：这次实际调用按单价算出的美元。模拟供应商是 0。
- 按当前路线预计：用剧本里的镜头数、时长和对白字数估算。
- 商用参考：同一规模如果走兼容 LLM + Seedream + Seedance + MiniMax + 可灵口型。

改单价只动 `pricing.py`。

## Cost

Metering is in `model_calls`. The watch page shows actual spend, a forecast for the selected providers, and a commercial reference quote. Mock calls cost zero. Edit `pricing.py` to match a contract.

## 测试

`backend/tests` 覆盖小说解析（txt/gb18030、docx、epub）、供应商请求形状（Seedance、Kling、MiniMax、ElevenLabs、通用轮询）、失败回退、定妆锁、镜头级放行，以及用示例小说跑出成片。

## Tests

`cd backend && pytest` covers parsing, adapter payload shapes, fallback, the character-lock gate, per-shot release, and a full mock render of the sample novel.

## 审片、报价、剪映、积分、定妆锚点

默认仍然是全自动。创建项目时可以打开两个检查点：先审剧本，或先确认角色定妆。没打开就一路做到成片。

制作板上每个镜头有「重做」。可以选择整镜、分镜、视频、配音或口型，也可以改一句画面提示。其他镜头不动。质检失败时的自动重试还在。

开拍按钮上方按当前模型路线列出编剧、定妆与分镜、视频、配音、口型、配乐的报价。模拟路线是 0，旁边仍有商用参考。成片页对照开拍报价和实际计量。单价在 `backend/app/adapters/pricing.py`。

导出两个压缩包：

- 剪映：`draft_content.json` 和 `draft_info.json`（时间是微秒，画布 9:16），加上视频、人声、字幕。这是剪映 5.9 / CapCut 本地明文草稿。剪映 6 及更新版本可能加密，打不开时用另一个包。
- 通用：`timeline.edl`、`timeline.fcpxml`、`subtitles.srt` 和素材。

积分按 API Key 区分用户。开发环境钥匙是 `zc-dev-user`（余额 100）和 `zc-dev-admin`（可在「积分」页充值）。开拍按报价乘以 `CREDIT_MARKUP`（默认 1.2）预扣，结束后按 `model_calls` 的实际金额结算，多退少补。流水在 `ledger_entries`。

每个角色有脸、发型、服装、色板四个锚点，写进定妆、分镜和视频提示，定妆图作为参考图传给每一次出图和出视频。视频提示里写明对白只在一段时间内张嘴，配音音轨按同一时间点铺上静音。

## Checkpoints, quotes, JianYing, credits, anchors

Checkpoints for the script and the character bible are off unless the project turns them on. The board can rerun one shot or one stage, with an optional prompt, and leave every other shot alone.

The form shows a per-stage quote before run. The watch page compares that quote with metered actuals. Prices live in `pricing.py`.

Export a JianYing/CapCut plain-JSON draft (`draft_content.json` and `draft_info.json`, microseconds, 9:16) or a generic EDL + FCPXML + SRT package. JianYing 6 and newer may encrypt drafts; use the generic package then.

`zc-dev-user` and `zc-dev-admin` are the local keys. A run holds `estimate × CREDIT_MARKUP` and settles on the metered total. Character anchors (face, hair, outfit, palette) are repeated in every image and video prompt, and the video prompt states the dialogue window used by the voice track.

## 范围

这一版只做一种风格。配乐只有本地模拟。默认定妆自动通过；勾选「需要人工确认」后，制作板会停在定妆页。单集最多 2 集、每集 6 个镜头，避免一次真实调用打满配额，可在项目 `settings` 里改 `max_episodes` 和 `max_shots_per_episode`。角色重生成只在分镜开始前开放。

## Scope

One style. Music is local. Bible approval is optional and off by default. Episode and shot caps default to 2 and 6. Character regeneration is available before storyboard starts.
