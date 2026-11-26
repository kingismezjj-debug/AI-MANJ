"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiGet, apiPost } from "@/lib/api";
import { statusLabel } from "@/lib/types";

interface Book {
  title: string;
  summary: string;
  bible: { characters?: { name?: string; notes?: string; phases?: string[] }[]; world?: string; timeline?: string[]; phases?: string[] };
  episode_count: number;
  produce_first: number;
  episodes: { id: string; number: number; title: string; synopsis: string; status: string; shots: number }[];
}

export default function BookPage() {
  const params = useParams<{ id: string }>();
  const [book, setBook] = useState<Book | null>(null);
  const [error, setError] = useState("");

  function load() {
    apiGet<Book>(`/api/projects/${params.id}/book`).then(setBook).catch((err: Error) => setError(err.message));
  }

  useEffect(() => {
    load();
  }, [params.id]);

  if (!book) return <p className="meta">{error || "正在打开故事圣经…"}</p>;
  const people = book.bible.characters || [];
  return (
    <main>
      <p className="kicker">长篇</p>
      <h2>{book.title || "故事圣经"}</h2>
      <p className="lead">{book.summary || book.bible.world}</p>
      <p className="meta">计划 {book.episode_count} 集，这一轮先写 {book.produce_first} 集。后面的集已有大纲，可以接着写。</p>
      <section className="panel">
        <h3>角色与阶段</h3>
        {people.length === 0 ? <p className="meta">还没有角色笔记。</p> : null}
        {people.map((person) => (
          <p key={person.name}>{person.name} · {(person.phases || []).join(" / ") || person.notes}</p>
        ))}
        <p className="meta">时间线 {(book.bible.timeline || []).join(" → ")}</p>
      </section>
      <section className="list">
        {book.episodes.map((episode) => (
          <article className="card" key={episode.id}>
            <div>
              <h3>第 {episode.number} 集 {episode.title}</h3>
              <p className="meta">{episode.synopsis} · {episode.shots} 个镜头</p>
            </div>
            <span className="pill">{statusLabel(episode.status)}</span>
          </article>
        ))}
      </section>
      <button className="btn" type="button" onClick={() => void apiPost(`/api/projects/${params.id}/continue`, { count: 1 }).then(load).catch((err: Error) => setError(err.message))}>
        再写下一集
      </button>
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
