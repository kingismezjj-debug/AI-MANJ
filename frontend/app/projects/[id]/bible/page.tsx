"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiGet, apiPost, mediaUrl } from "@/lib/api";
import { statusLabel, type Board } from "@/lib/types";

export default function BiblePage() {
  const params = useParams<{ id: string }>();
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    setBoard(await apiGet<Board>(`/api/projects/${params.id}`));
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
    const timer = window.setInterval(() => refresh().catch(() => undefined), 2500);
    return () => window.clearInterval(timer);
  }, [params.id]);

  async function act(path: string) {
    setError("");
    try {
      await apiPost(path);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  if (!board) return <p className="meta">{error || "正在读取定妆…"}</p>;

  return (
    <main>
      <div className="board-head">
        <div>
          <p className="kicker">视觉圣经</p>
          <h2>角色先锁定，镜头才开画</h2>
          <p className="activity">定妆图是后续分镜和视频的硬依赖。确认之前，镜头不会往下走。</p>
        </div>
        <div className="links">
          <a href={`/projects/${params.id}`}>返回制作板</a>
          {board.project.status === "awaiting_approval" ? (
            <button className="btn" type="button" onClick={() => void act(`/api/projects/${params.id}/characters/approve`)}>
              全部通过
            </button>
          ) : null}
        </div>
      </div>
      {board.characters.length === 0 ? <p className="meta">编剧还没有交出角色。</p> : null}
      <div className="cast">
        {board.characters.map((character) => (
          <article key={character.id}>
            <div className="portrait">
              {character.image_url ? <img src={mediaUrl(character.image_url)} alt={character.name} /> : <span className="meta">绘制中</span>}
            </div>
            <h3>{character.name}</h3>
            <p>{character.appearance}</p>
            <p>种子 {character.seed} · {statusLabel(character.status)}{character.locked ? " · 已锁定" : ""}</p>
            <div className="row">
              {!character.locked && character.image_url ? (
                <button className="btn" type="button" onClick={() => void act(`/api/characters/${character.id}/approve`)}>
                  通过
                </button>
              ) : null}
              <button className="btn secondary" type="button" onClick={() => void act(`/api/characters/${character.id}/regenerate`)}>
                重新生成
              </button>
            </div>
          </article>
        ))}
        {board.locations.map((location) => (
          <article key={location.id}>
            <div className="portrait">
              {location.image_url ? <img src={mediaUrl(location.image_url)} alt={location.name} /> : null}
            </div>
            <h3>{location.name}</h3>
            <p>{location.description}</p>
          </article>
        ))}
      </div>
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
