"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost, apiUpload } from "@/lib/api";
import { money, statusLabel, type ProjectSummary } from "@/lib/types";

export default function HomePage() {
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [title, setTitle] = useState("雨夜来信");
  const [preset, setPreset] = useState("mock");
  const [approval, setApproval] = useState(false);
  const [scriptGate, setScriptGate] = useState(false);
  const [quote, setQuote] = useState<{ total_usd: number; draft_usd?: number; final_usd?: number; reference_usd: number; assumption: string; lines: { stage: string; provider: string; detail: string; usd: number }[] } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  async function refresh() {
    setProjects(await apiGet<ProjectSummary[]>("/api/projects"));
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
    apiPost<typeof quote>("/api/estimates", { preset, use_sample: true })
      .then((value) => setQuote(value))
      .catch(() => undefined);
  }, [preset]);

  async function createAndRun(useSample: boolean) {
    setBusy(useSample ? "sample" : "upload");
    setError("");
    try {
      const project = await apiPost<ProjectSummary>("/api/projects", {
        title: title || "未命名项目",
        preset,
        require_bible_approval: approval,
        require_script_approval: scriptGate,
      });
      if (useSample) {
        await apiPost(`/api/projects/${project.id}/novel/sample`);
      } else if (file) {
        await apiUpload(`/api/projects/${project.id}/novel`, file);
      } else {
        throw new Error("请选择 txt、docx 或 epub 文件");
      }
      await apiPost(`/api/projects/${project.id}/run`);
      router.push(`/projects/${project.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "开拍失败");
    } finally {
      setBusy("");
    }
  }

  return (
    <main>
      <section className="hero">
        <div className="intro">
          <h2>一部小说，一次开拍。</h2>
          <p className="lead">
            帧川把小说收成镜头，先锁定角色定妆，再让每个镜头自己往下走：分镜、预演、视频、配音、口型、字幕和成片可以同时推进。
          </p>
          <div className="steps">
            <span>读入</span>
            <span>编剧</span>
            <span>定妆</span>
            <span>分镜</span>
            <span>视频</span>
            <span>口型</span>
            <span>成片</span>
          </div>
        </div>
        <form
          className="panel"
          onSubmit={(event) => {
            event.preventDefault();
            void createAndRun(false);
          }}
        >
          <h3>开一个项目</h3>
          <label htmlFor="title">项目名称</label>
          <input id="title" value={title} onChange={(event) => setTitle(event.target.value)} />
          <label htmlFor="preset">模型路线</label>
          <select id="preset" value={preset} onChange={(event) => setPreset(event.target.value)}>
            <option value="mock">本地模拟（无需密钥）</option>
            <option value="cn_stack">国内商用：Seedream / Seedance / MiniMax / 可灵</option>
            <option value="global_stack">海外：兼容图片 / Kling / ElevenLabs</option>
          </select>
          <label htmlFor="novel">小说文件</label>
          <input
            id="novel"
            type="file"
            accept=".txt,.docx,.epub,text/plain"
            hidden
            onChange={(event) => setFile(event.target.files?.[0] || null)}
          />
          <label className="btn secondary file-btn" htmlFor="novel">
            {file ? file.name : "选择 txt / docx / epub"}
          </label>
          <label className="check">
            <input type="checkbox" checked={scriptGate} onChange={(event) => setScriptGate(event.target.checked)} />
            先审剧本再往下做（默认关闭）
          </label>
          <label className="check">
            <input type="checkbox" checked={approval} onChange={(event) => setApproval(event.target.checked)} />
            角色定妆需要人工确认后再生成镜头
          </label>
          {quote ? (
            <div className="quote">
              <p className="meta">成片报价 {money(quote.final_usd ?? quote.total_usd)} · 低清样片 {money(quote.draft_usd ?? quote.total_usd)} · 商用参考 {money(quote.reference_usd)}</p>
              <ul>
                {quote.lines.map((line) => (
                  <li key={line.stage}>
                    <span>{line.stage} · {line.provider} · {line.detail}</span>
                    <span>{money(line.usd)}</span>
                  </li>
                ))}
              </ul>
              <p className="note">{quote.assumption}</p>
            </div>
          ) : null}
          <div className="row">
            <button className="btn" type="button" disabled={!!busy} onClick={() => void createAndRun(true)}>
              {busy === "sample" ? "正在开拍…" : "用《雨夜来信》开拍"}
            </button>
            <button className="btn secondary" type="submit" disabled={!!busy}>
              {busy === "upload" ? "正在上传…" : "上传并开拍"}
            </button>
          </div>
          {error ? <p className="error">{error}</p> : null}
        </form>
      </section>

      <section className="list">
        {projects.length === 0 ? <p className="meta">还没有项目。</p> : null}
        {projects.map((project) => (
          <a className="card" key={project.id} href={`/projects/${project.id}`}>
            <div>
              <h3>{project.title}</h3>
              <div className="meta">
                <span>{project.created_at ? new Date(project.created_at).toLocaleString() : ""}</span>
                <span>{project.progress?.label || statusLabel(project.status)}{project.progress?.percent != null ? ` · ${Math.round(project.progress.percent)}%` : ""}</span>
                <span>已计量 {money(project.costs?.actual_usd)}</span>
              </div>
            </div>
            <span className={`pill ${project.status === "completed" ? "ok" : project.status === "failed" ? "bad" : project.status === "running" ? "run" : "wait"}`}>
              {statusLabel(project.status)}
            </span>
          </a>
        ))}
      </section>
    </main>
  );
}
