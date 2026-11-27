"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiGet, apiPost } from "@/lib/api";

interface AssetCard {
  id: string;
  kind: string;
  name: string;
  description: string;
  prompt: string;
  version: number;
  status: string;
  views: Record<string, string>;
  versions: { number: number; note: string }[];
}

const KIND: Record<string, string> = { character: "角色", scene: "场景", prop: "道具", costume: "服装" };

export default function AssetsPage() {
  const params = useParams<{ id: string }>();
  const [assets, setAssets] = useState<AssetCard[]>([]);
  const [library, setLibrary] = useState<AssetCard[]>([]);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  function load() {
    apiGet<{ assets: AssetCard[] }>(`/api/projects/${params.id}/assets`).then((body) => setAssets(body.assets)).catch((err: Error) => setError(err.message));
    apiGet<{ assets: AssetCard[] }>("/api/library/assets").then((body) => setLibrary(body.assets)).catch(() => undefined);
  }

  useEffect(() => {
    load();
  }, [params.id]);

  return (
    <main>
      <p className="kicker">资产库</p>
      <h2>角色、场景、道具</h2>
      <p className="lead">改一张卡，用到它的镜头会标成待更新，可以一次重做。角色出三视图，场景出四视图。卡片可以放进跨项目库再导入。</p>
      {assets.map((asset) => (
        <section className="panel" key={asset.id} style={{ marginBottom: 14 }}>
          <h3>{KIND[asset.kind] || asset.kind} · {asset.name} · 第 {asset.version} 版</h3>
          <p className="meta">{asset.description}</p>
          <label>提示词</label>
          <textarea defaultValue={asset.prompt} onBlur={(event) => {
            if (event.target.value !== asset.prompt) {
              void apiPost(`/api/assets/${asset.id}`, { prompt: event.target.value }).then(() => { setNote("已保存，相关镜头待更新"); load(); });
            }
          }} />
          <div className="row">
            <button className="btn secondary" type="button" onClick={() => void apiPost(`/api/assets/${asset.id}/views`).then(load).catch((err: Error) => setError(err.message))}>生成视图</button>
            <button className="btn secondary" type="button" onClick={() => void apiPost(`/api/assets/${asset.id}/regenerate`).then(() => setNote("已排队重做受影响的镜头")).catch((err: Error) => setError(err.message))}>重做受影响镜头</button>
            {asset.versions.filter((item) => item.number < asset.version).slice(-3).map((item) => (
              <button className="btn secondary" key={item.number} type="button" onClick={() => void apiPost(`/api/assets/${asset.id}/rollback`, { version: item.number }).then(load)}>回到第 {item.number} 版</button>
            ))}
            <button className="btn secondary" type="button" onClick={() => void apiPost("/api/library/assets", { kind: asset.kind, name: asset.name, description: asset.description, prompt: asset.prompt }).then(load)}>放入跨项目库</button>
          </div>
          <p className="meta">{Object.keys(asset.views).length ? `已有视图：${Object.keys(asset.views).join("、")}` : "还没有视图"}</p>
        </section>
      ))}
      {library.length ? (
        <section className="panel">
          <h3>跨项目库</h3>
          {library.map((asset) => (
            <div className="row" key={asset.id}>
              <span>{asset.name}</span>
              <button className="btn secondary" type="button" onClick={() => void apiPost(`/api/projects/${params.id}/assets/import`, { asset_id: asset.id }).then(load)}>导入本项目</button>
            </div>
          ))}
        </section>
      ) : null}
      {note ? <p className="meta">{note}</p> : null}
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
