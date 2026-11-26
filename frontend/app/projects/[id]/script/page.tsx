"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiGet, apiPost, apiPut } from "@/lib/api";

interface ShotRow {
  id: string;
  number: number;
  description: string;
  dialogue: string;
  speaker: string;
  visual_prompt: string;
  stale: boolean;
}

interface Script {
  episodes: { id: string; number: number; title: string; status: string; shots: ShotRow[] }[];
}

interface Hunk {
  tag: string;
  before: string;
  after: string;
}

export default function ScriptPage() {
  const params = useParams<{ id: string }>();
  const [script, setScript] = useState<Script | null>(null);
  const [instruction, setInstruction] = useState("写得更短");
  const [proposal, setProposal] = useState<{ shotId: string; field: string; before: string; proposed: string; hunks: Hunk[]; affected: string[] } | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [error, setError] = useState("");

  function load() {
    apiGet<Script>(`/api/projects/${params.id}/script`).then(setScript).catch((err: Error) => setError(err.message));
  }

  useEffect(() => {
    load();
  }, [params.id]);

  if (!script) return <p className="meta">{error || "正在打开剧本…"}</p>;

  return (
    <main>
      <p className="kicker">剧本与分镜</p>
      <h2>改一处，后面的镜头会标成待更新</h2>
      {script.episodes.map((episode) => (
        <section key={episode.id} className="panel" style={{ marginBottom: 14 }}>
          <h3>第 {episode.number} 集 {episode.title}</h3>
          {episode.shots.map((shot) => (
            <article key={shot.id} style={{ marginTop: 12 }}>
              <p className="meta">镜头 {shot.number} {shot.speaker ? `· ${shot.speaker}` : ""} {shot.stale ? "· 待更新" : ""}</p>
              <label>画面</label>
              <textarea defaultValue={shot.description} onBlur={(event) => {
                if (event.target.value !== shot.description) {
                  void apiPut(`/api/shots/${shot.id}`, { description: event.target.value }).then(load);
                }
              }} />
              <label>对白</label>
              <textarea defaultValue={shot.dialogue} onBlur={(event) => {
                if (event.target.value !== shot.dialogue) {
                  void apiPut(`/api/shots/${shot.id}`, { dialogue: event.target.value }).then(load);
                }
              }} />
              <div className="row">
                <button className="btn secondary" type="button" onClick={() => void apiPost<{ before: string; proposed: string; hunks: Hunk[]; affected: { shot_ids: string[] } }>(`/api/projects/${params.id}/rewrite`, { shot_id: shot.id, field: "description", instruction, text: shot.description }).then((body) => { setProposal({ shotId: shot.id, field: "description", before: body.before, proposed: body.proposed, hunks: body.hunks, affected: body.affected.shot_ids }); setPicked(body.hunks.map((_, index) => index)); }).catch((err: Error) => setError(err.message))}>让模型改画面</button>
                <button className="btn secondary" type="button" onClick={() => void apiPost(`/api/shots/${shot.id}/upscale`).then(load).catch((err: Error) => setError(err.message))}>做成成片</button>
              </div>
            </article>
          ))}
        </section>
      ))}
      <label>改写要求</label>
      <input value={instruction} onChange={(event) => setInstruction(event.target.value)} />
      {proposal ? (
        <section className="panel">
          <h3>对照</h3>
          <p className="meta">会影响 {proposal.affected.length} 个镜头。</p>
          {proposal.hunks.map((hunk, index) => (
            <label className="check" key={index}>
              <input type="checkbox" checked={picked.includes(index)} onChange={(event) => setPicked(event.target.checked ? [...picked, index] : picked.filter((item) => item !== index))} />
              <span style={{ color: "#c45c4a" }}>{hunk.before}</span>
              <span style={{ color: "#3d7a4a" }}>{hunk.after}</span>
            </label>
          ))}
          <div className="row">
            <button className="btn" type="button" onClick={() => void apiPost(`/api/projects/${params.id}/rewrite/accept`, { shot_id: proposal.shotId, field: proposal.field, before: proposal.before, proposed: proposal.proposed, hunks: picked }).then(() => { setProposal(null); load(); }).catch((err: Error) => setError(err.message))}>采用勾选的改动</button>
            <button className="btn secondary" type="button" onClick={() => setProposal(null)}>放弃</button>
          </div>
        </section>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
