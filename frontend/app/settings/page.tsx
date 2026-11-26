"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost, apiPut } from "@/lib/api";

interface Slot {
  category: string;
  provider: string;
  base_url: string;
  model: string;
  params: Record<string, unknown>;
  enabled: boolean;
  api_key_set: boolean;
  api_key_mask: string;
  key_source: string;
  providers: string[];
}

const LABELS: Record<string, string> = {
  llm: "大脑 / 编剧",
  image: "文生图",
  video: "视频",
  tts: "配音",
  lipsync: "口型",
  music: "配乐",
};

const HINTS: Record<string, string> = {
  llm: "默认 DeepSeek。Base URL 填 https://api.deepseek.com/v1 ，模型填 deepseek-chat 或 deepseek-reasoner。自建中转同样走 OpenAI 兼容。",
  image: "Seedream 走火山方舟，或任何 OpenAI 兼容的 /images/generations。",
  video: "Seedance、可灵，或通用 HTTP 轮询。",
};

export default function SettingsPage() {
  const [slots, setSlots] = useState<Slot[]>([]);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [paramsText, setParamsText] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [results, setResults] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  function apply(next: Slot[]) {
    setSlots(next);
    const text: Record<string, string> = {};
    next.forEach((slot) => {
      text[slot.category] = JSON.stringify(slot.params || {}, null, 0);
    });
    setParamsText(text);
  }

  useEffect(() => {
    apiGet<{ slots: Slot[] }>("/api/settings/slots")
      .then((body) => apply(body.slots))
      .catch((err: Error) => setError(err.message));
  }, []);

  function update(category: string, patch: Partial<Slot>) {
    setSlots((current) => current.map((slot) => (slot.category === category ? { ...slot, ...patch } : slot)));
  }

  async function save(group: Slot[]) {
    setError("");
    const payload = group.map((slot) => ({
      ...slot,
      api_key: keys[slot.category] || "",
      params: JSON.parse(paramsText[slot.category] || "{}"),
    }));
    const saved = await apiPut<{ slots: Slot[] }>("/api/settings/slots", { slots: payload });
    apply(saved.slots);
    setKeys({});
    setMessage("已保存，下一次调用立即生效。");
  }

  async function test(slot: Slot) {
    setError("");
    const result = await apiPost<{ ok: boolean; latency_ms: number; detail: string; error: string }>("/api/settings/slots/test", {
      category: slot.category,
      provider: slot.provider,
      base_url: slot.base_url,
      model: slot.model,
      api_key: keys[slot.category] || "",
      params: JSON.parse(paramsText[slot.category] || "{}"),
    });
    setResults((current) => ({
      ...current,
      [slot.category]: result.ok ? `接通 ${result.latency_ms} ms · ${result.detail}` : `失败 ${result.latency_ms} ms · ${result.error}`,
    }));
  }

  const primary = slots.filter((slot) => ["llm", "image", "video"].includes(slot.category));
  const secondary = slots.filter((slot) => !["llm", "image", "video"].includes(slot.category));

  return (
    <main>
      <p className="kicker">接口配置</p>
      <h2>模型设置</h2>
      <p className="lead">三条主接口：总控和编剧共用大脑，文生图出定妆和分镜，视频出镜头。密钥只存在服务器上，页面只显示掩码。选「本地模拟」可以不填密钥跑完整条流水线。</p>
      {primary.map((slot) => (
        <SlotCard key={slot.category} slot={slot} hint={HINTS[slot.category]} secret={keys[slot.category] || ""} params={paramsText[slot.category] || "{}"} result={results[slot.category] || ""} onChange={update} onSecret={(value) => setKeys({ ...keys, [slot.category]: value })} onParams={(value) => setParamsText({ ...paramsText, [slot.category]: value })} onTest={() => void test(slot).catch((err: Error) => setError(err.message))} />
      ))}
      <details className="panel">
        <summary>配音、口型、配乐（默认本地模拟）</summary>
        {secondary.map((slot) => (
          <SlotCard key={slot.category} slot={slot} hint="" secret={keys[slot.category] || ""} params={paramsText[slot.category] || "{}"} result={results[slot.category] || ""} onChange={update} onSecret={(value) => setKeys({ ...keys, [slot.category]: value })} onParams={(value) => setParamsText({ ...paramsText, [slot.category]: value })} onTest={() => void test(slot).catch((err: Error) => setError(err.message))} />
        ))}
      </details>
      <div className="row">
        <button className="btn" type="button" onClick={() => void save(slots).catch((err: Error) => setError(err.message))}>保存全部</button>
      </div>
      {message ? <p className="meta">{message}</p> : null}
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}

function SlotCard({
  slot,
  hint,
  secret,
  params,
  result,
  onChange,
  onSecret,
  onParams,
  onTest,
}: {
  slot: Slot;
  hint: string;
  secret: string;
  params: string;
  result: string;
  onChange: (category: string, patch: Partial<Slot>) => void;
  onSecret: (value: string) => void;
  onParams: (value: string) => void;
  onTest: () => void;
}) {
  return (
    <section className="panel" style={{ marginBottom: 14 }}>
      <h3>{LABELS[slot.category] || slot.category}</h3>
      {hint ? <p className="meta">{hint}</p> : null}
      <label>供应商</label>
      <select value={slot.provider} onChange={(event) => onChange(slot.category, { provider: event.target.value })}>
        {slot.providers.map((provider) => (
          <option key={provider} value={provider}>{provider === "mock" ? "本地模拟" : provider === "openai_compatible" ? "OpenAI 兼容 / DeepSeek / 中转" : provider}</option>
        ))}
      </select>
      <label>Base URL</label>
      <input value={slot.base_url} onChange={(event) => onChange(slot.category, { base_url: event.target.value })} placeholder="https://api.deepseek.com/v1" />
      <label>API Key {slot.api_key_set ? `（已保存 ${slot.api_key_mask}${slot.key_source === "env" ? "，来自环境变量" : ""}）` : "（未设置）"}</label>
      <input value={secret} type="password" autoComplete="off" placeholder={slot.api_key_set ? "留空则保留原密钥" : "sk-..."} onChange={(event) => onSecret(event.target.value)} />
      <label>模型名</label>
      <input value={slot.model} onChange={(event) => onChange(slot.category, { model: event.target.value })} placeholder="deepseek-chat" />
      <label>额外参数 JSON</label>
      <textarea value={params} onChange={(event) => onParams(event.target.value)} />
      <div className="row">
        <button className="btn secondary" type="button" onClick={onTest}>测试连接</button>
      </div>
      {result ? <p className={result.startsWith("失败") ? "error" : "meta"}>{result}</p> : null}
    </section>
  );
}
