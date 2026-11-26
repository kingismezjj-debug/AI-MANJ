"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost, getKey, setKey } from "@/lib/api";

interface Account {
  id: string;
  name: string;
  role: string;
  credit_balance: number;
}

export default function AdminPage() {
  const [key, setKeyDraft] = useState("");
  const [users, setUsers] = useState<Account[]>([]);
  const [amount, setAmount] = useState("20");
  const [error, setError] = useState("");
  const [me, setMe] = useState<Account | null>(null);

  async function load() {
    setMe(await apiGet<Account>("/api/me"));
    if (getKey() === "zc-dev-admin") {
      setUsers(await apiGet<Account[]>("/api/admin/users"));
    }
  }

  useEffect(() => {
    setKeyDraft(getKey());
    load().catch((err: Error) => setError(err.message));
  }, []);

  return (
    <main>
      <p className="kicker">工作室账户</p>
      <h2>积分</h2>
      <p className="meta">当前 {me ? `${me.name} · 余额 ${me.credit_balance.toFixed(3)}` : "未登录"}。开发环境默认密钥是 zc-dev-user，管理员是 zc-dev-admin。</p>
      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault();
          setKey(key);
          load().catch((err: Error) => setError(err.message));
        }}
      >
        <label htmlFor="key">API Key</label>
        <input id="key" value={key} onChange={(event) => setKeyDraft(event.target.value)} />
        <div className="row">
          <button className="btn" type="submit">使用这把钥匙</button>
        </div>
      </form>
      {users.length > 0 ? (
        <section className="panel" style={{ marginTop: 16 }}>
          <h3>充值</h3>
          {users.map((user) => (
            <form
              key={user.id}
              className="row"
              onSubmit={(event) => {
                event.preventDefault();
                void apiPost("/api/admin/topup", { user_id: user.id, amount: Number(amount), note: "手工充值" })
                  .then(() => load())
                  .catch((err: Error) => setError(err.message));
              }}
            >
              <span>{user.name} · {user.credit_balance.toFixed(3)}</span>
              <input value={amount} onChange={(event) => setAmount(event.target.value)} />
              <button className="btn secondary" type="submit">充入</button>
            </form>
          ))}
        </section>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}
