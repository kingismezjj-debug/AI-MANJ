"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiGet, mediaUrl } from "@/lib/api";
import { money, type Board } from "@/lib/types";

const CATEGORY: Record<string, string> = {
  llm: "语言模型",
  image: "图像",
  video: "视频",
  tts: "语音",
  lipsync: "口型",
  music: "配乐",
};

export default function WatchPage() {
  const params = useParams<{ id: string }>();
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    apiGet<Board>(`/api/projects/${params.id}`)
      .then(setBoard)
      .catch((err: Error) => setError(err.message));
  }, [params.id]);

  if (!board) return <p className="meta">{error || "正在读取成片…"}</p>;
  const video = mediaUrl(board.project.final_video_url);
  const subtitle = mediaUrl(board.project.subtitle_url);

  return (
    <main>
      <div className="board-head">
        <div>
          <p className="kicker">成片</p>
          <h2>{board.project.title}</h2>
          <p className="activity">
            {board.project.final_meta?.burned_subtitles ? "字幕已烧录" : "字幕文件已生成"}
            {board.project.final_meta?.ai_label ? " · 含 AI 生成标识" : ""}
          </p>
        </div>
        <div className="links">
          <a href={`/projects/${params.id}`}>制作板</a>
          {video ? <a href={video}>下载视频</a> : null}
          {subtitle ? <a href={subtitle}>下载字幕</a> : null}
        </div>
      </div>
      <div className="player">
        {video ? <video src={video} controls playsInline /> : <p className="meta" style={{ padding: 24 }}>成片还在后面的工序里。</p>}
      </div>
      <section className="panel" style={{ marginTop: 18 }}>
        <h3>费用估算</h3>
        <div className="meta">
          <span>已计量 {money(board.costs.actual_usd)}</span>
          <span>按当前模型预计 {money(board.costs.estimated_usd)}{board.costs.preliminary ? "（尚无剧本，按样片规模估算）" : ""}</span>
          <span>若换成商用组合 {money(board.costs.reference_usd)}</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>类别</th>
              <th>调用</th>
              <th>已计量</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(board.costs.by_category).map(([key, value]) => (
              <tr key={key}>
                <td>{CATEGORY[key] || key}</td>
                <td>{value.calls}</td>
                <td>{money(value.usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table>
          <thead>
            <tr>
              <th>最近调用</th>
              <th>供应商</th>
              <th>状态</th>
              <th>费用</th>
            </tr>
          </thead>
          <tbody>
            {board.costs.calls.slice(0, 12).map((call) => (
              <tr key={call.id}>
                <td>{CATEGORY[call.category] || call.category}</td>
                <td>{call.provider}</td>
                <td>{call.status}</td>
                <td>{money(call.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="note">{board.costs.note}</p>
        {error ? <p className="error">{error}</p> : null}
      </section>
    </main>
  );
}
