# 帧川 Zhenchuan

小说进，漫剧出。上传一本短篇，帧川按镜头把竖屏 AI 漫剧做完：编剧、角色定妆、分镜、预演、视频、配音、口型、字幕和成片。镜头是工作单位。上游一就绪，这个镜头就往下走，不必等整集。

默认画幅是 2D 动漫、9:16、720×1280。不配置任何密钥时，全流程用本地模拟供应商跑完，并产出带字幕和「AI生成」标识的 mp4。

A novel goes in, a vertical AI short comes out. The unit of work is the shot. As soon as one shot's upstream artifact exists, that shot moves on. The default picture is 2D anime, 9:16, 720×1280. With no API keys, mock providers render the whole film locally.

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

## 范围

这一版只做一种风格。配乐只有本地模拟。默认定妆自动通过；勾选「需要人工确认」后，制作板会停在定妆页。单集最多 2 集、每集 6 个镜头，避免一次真实调用打满配额，可在项目 `settings` 里改 `max_episodes` 和 `max_shots_per_episode`。角色重生成只在分镜开始前开放。

## Scope

One style. Music is local. Bible approval is optional and off by default. Episode and shot caps default to 2 and 6. Character regeneration is available before storyboard starts.
