"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API_BASE, apiGet, apiPost, getKey, mediaUrl } from "@/lib/api";
import { SHOT_STAGES, money, statusLabel, type Board } from "@/lib/types";

function ShotRegen({ shotId, onDone }: { shotId: string; onDone: () => void }) {
  const [stage, setStage] = useState("shot");
  const [prompt, setPrompt] = useState("");
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button className="btn secondary regen" type="button" onClick={() => setOpen(true)}>
        重做
      </button>
    );
  }
  return (
    <form
      className="regen"
      onSubmit={(event) => {
        event.preventDefault();
        void apiPost(`/api/shots/${shotId}/regenerate`, { stage, prompt: prompt || null }).then(onDone);
      }}
    >
      <select value={stage} onChange={(event) => setStage(event.target.value)} aria-label="重做工序">
        <option value="shot">整镜</option>
        <option value="storyboard">分镜</option>
        <option value="video">视频</option>
        <option value="voice">配音</option>
        <option value="lipsync">口型</option>
      </select>
      <textarea value={prompt} placeholder="可选：改画面或对白" onChange={(event) => setPrompt(event.target.value)} />
      <button className="btn" type="submit">开始</button>
    </form>
  );
}

export default function BoardPage() {
  const params = useParams<{ id: string }>();
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    setBoard(await apiGet<Board>(`/api/projects/${params.id}`));
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
    const timer = window.setInterval(() => {
      refresh().catch(() => undefined);
    }, 2000);
    const source = new EventSource(`${API_BASE}/api/projects/${params.id}/events?k=${encodeURIComponent(getKey())}`);
    source.onmessage = () => {
      refresh().catch(() => undefined);
    };
    return () => {
      window.clearInterval(timer);
      source.close();
    };
  }, [params.id]);

  if (!board) {
    return <p className="meta">{error || "正在打开制作板…"}</p>;
  }
  const total = Object.values(board.counts).reduce((sum, value) => sum + value, 0);
  const done = board.counts.succeeded || 0;
  const progress = board.progress;
  const ratio = progress?.percent ?? (total ? Math.round((done / total) * 100) : 0);
  const stalled = progress?.stalled;
  const interrupted = progress?.state === "interrupted" || board.project.status === "interrupted";
  const failed = progress?.state === "failed" || board.project.status === "failed";

  return (
    <main>
      <div className="board-head">
        <div>
          <p className="kicker">制作板 · {board.project.aspect_ratio} · 二次元</p>
          <h2>{board.project.title}</h2>
          <p className="activity">
            {progress?.label || (board.activity ? `正在${board.activity.label}` : statusLabel(board.project.status))}
            {typeof ratio === "number" && !progress?.indeterminate ? ` · ${Math.round(ratio)}%` : ""}
          </p>
        </div>
        <div className="links">
          <a href={`/projects/${params.id}/book`}>故事圣经</a>
          <a href={`/projects/${params.id}/script`}>剧本</a>
          <a href={`/projects/${params.id}/assets`}>资产</a>
          <a href={`/projects/${params.id}/agents`}>Agent 协作</a>
          <a href={`/projects/${params.id}/bible`}>角色定妆</a>
          <a href={`/projects/${params.id}/watch`}>成片与费用</a>
          <button className="btn secondary" type="button" onClick={() => void apiPost<{ url: string }>(`/api/projects/${params.id}/grid`).then((body) => window.open(mediaUrl(body.url), "_blank")).catch((err: Error) => setError(err.message))}>分镜格</button>
          {failed || interrupted ? (
            <button className="btn secondary" type="button" onClick={() => void apiPost(`/api/projects/${params.id}/run`).then(() => refresh())}>
              {interrupted ? "继续" : "重试"}
            </button>
          ) : null}
        </div>
      </div>
      <div className="meta">
        <span className={`pill ${board.project.status === "completed" ? "ok" : failed || interrupted ? "bad" : stalled ? "bad" : "run"}`}>
          {interrupted ? "已中断" : failed ? "出错" : stalled ? "疑似卡住" : statusLabel(board.project.status)}
        </span>
        {progress?.seconds_ago != null ? <span>最近一次活动 {progress.seconds_ago} 秒前</span> : null}
        {progress?.last_call ? <span>上次调用 {progress.last_call.model || progress.last_call.provider} · {progress.last_call.status}</span> : null}
        {progress?.eta_sec ? <span>大约还要 {Math.max(1, Math.round(progress.eta_sec / 60))} 分钟</span> : null}
        <span>已完成 {done}/{total || 0}</span>
        <span>已计量 {money(board.costs.actual_usd)}</span>
        <span>按当前路线预计 {money(board.costs.estimated_usd)}</span>
        <span>商用参考 {money(board.costs.reference_usd)}</span>
        {board.costs.quote ? <span>开拍报价 {money(board.costs.quote.total_usd)}</span> : null}
      </div>
      {(failed || interrupted) && (progress?.error || board.project.error) ? <p className="error">{progress?.error || board.project.error}</p> : null}
      {stalled ? <p className="error">已经 {progress?.seconds_ago} 秒没有新的模型调用。可以等它自己恢复，或者看工人日志。</p> : null}
      <div className={`bar ${progress?.indeterminate ? "wait" : ""}`} aria-hidden>
        <div style={{ width: progress?.indeterminate ? "100%" : `${ratio}%` }} />
      </div>

      {board.project.status === "awaiting_script" ? (
        <section className="panel">
          <h3>剧本待确认</h3>
          <p className="meta">默认全自动。这个项目打开了剧本确认，通过之后才会定妆和分镜。</p>
          <button className="btn" type="button" onClick={() => void apiPost(`/api/projects/${params.id}/script/approve`).then(() => refresh())}>
            通过剧本
          </button>
        </section>
      ) : null}
      {board.episodes.length === 0 ? <p className="meta">编剧还没交剧本。镜头会在剧本落下后陆续出现。</p> : null}
      {board.episodes.map((episode) => (
        <section className="episode" key={episode.id}>
          <header>
            <div>
              <div className="kicker">第 {episode.number} 集</div>
              <h3>{episode.title}</h3>
            </div>
            <span className="pill">{episode.progress || statusLabel(episode.status)}</span>
          </header>
          <p className="hook">钩子：{episode.hook || "—"} · 悬念：{episode.cliffhanger || "—"}</p>
          <div className="film">
            {episode.shots.map((shot) => (
              <article className="shot" key={shot.id}>
                <div className="frame">
                  <span className="num">{String(shot.number).padStart(2, "0")}</span>
                  {shot.thumbnail_url ? <img src={mediaUrl(shot.thumbnail_url)} alt="" /> : <span className="meta">等待分镜</span>}
                </div>
                <div className="body">
                  <div className="who">{shot.speaker || shot.camera} · {shot.duration_sec.toFixed(1)}s</div>
                  <div className="line">{shot.dialogue || shot.description}</div>
                  <div className="pips">
                    {SHOT_STAGES.map((stage) => {
                      const status = shot.stages[stage.key] || "blocked";
                      const klass = status === "succeeded" ? "on" : status === "running" || status === "queued" ? "busy" : status === "failed" ? "bad" : "";
                      return (
                        <span className={`pip ${klass}`} key={stage.key} title={statusLabel(status)}>
                          {stage.label}
                        </span>
                      );
                    })}
                  </div>
                  <ShotRegen shotId={shot.id} onDone={() => void refresh()} />
                </div>
              </article>
            ))}
          </div>
        </section>
      ))}

      <section className="panel log">
        <h3>最近的工序</h3>
        <ul>
          {board.recent_jobs.map((job) => (
            <li key={job.id}>
              <strong>{job.label}</strong>
              <span>
                {statusLabel(job.status)}
                {job.error ? ` · ${job.error}` : ""}
              </span>
            </li>
          ))}
        </ul>
      </section>
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
