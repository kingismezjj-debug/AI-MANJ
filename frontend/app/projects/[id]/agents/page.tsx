"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API_BASE, apiGet, getKey } from "@/lib/api";
import { money, statusLabel } from "@/lib/types";

interface AgentDesk {
  agents: { id: string; label: string; status: string; jobs: number }[];
  messages: { id: string; label: string; kind: string; content: string; status: string }[];
  calls: { id: string; category: string; provider: string; model: string; status: string; latency_ms: number; cost_usd: number; error: string | null; summary: string }[];
}

export default function AgentsPage() {
  const params = useParams<{ id: string }>();
  const [desk, setDesk] = useState<AgentDesk | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    setDesk(await apiGet<AgentDesk>(`/api/projects/${params.id}/agents`));
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
    const timer = window.setInterval(() => refresh().catch(() => undefined), 2000);
    const source = new EventSource(`${API_BASE}/api/projects/${params.id}/events?k=${encodeURIComponent(getKey())}`);
    source.onmessage = () => {
      refresh().catch(() => undefined);
    };
    return () => {
      window.clearInterval(timer);
      source.close();
    };
  }, [params.id]);

  if (!desk) return <p className="meta">{error || "正在打开协作台…"}</p>;

  return (
    <main>
      <p className="kicker">Agent 协作</p>
      <h2>总控在派活</h2>
      <p className="meta">总控用「大脑 / 编剧」那条接口读小说、定规模，再把任务派给编剧、定妆、分镜、视频、配音和质检。下面是它发出的消息，以及每一次真实的模型调用。</p>
      <div className="cast">
        {desk.agents.map((agent) => (
          <article key={agent.id}>
            <h3>{agent.label}</h3>
            <p>{statusLabel(agent.status)} · {agent.jobs} 个任务</p>
          </article>
        ))}
      </div>
      <section className="panel log" style={{ marginTop: 16 }}>
        <h3>总控消息</h3>
        <ul>
          {desk.messages.map((item) => (
            <li key={item.id}>
              <strong>{item.label}</strong>
              <span>{item.content}</span>
            </li>
          ))}
        </ul>
      </section>
      <section className="panel" style={{ marginTop: 16 }}>
        <h3>接口调用</h3>
        <table>
          <thead>
            <tr>
              <th>接口</th>
              <th>供应商 / 模型</th>
              <th>耗时</th>
              <th>费用</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {desk.calls.map((call) => (
              <tr key={call.id}>
                <td>{call.category}</td>
                <td>{call.provider} {call.model}<div className="note">{call.summary || call.error}</div></td>
                <td>{call.latency_ms} ms</td>
                <td>{money(call.cost_usd)}</td>
                <td>{call.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <p className="meta"><a href={`/projects/${params.id}`}>返回制作板</a></p>
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
