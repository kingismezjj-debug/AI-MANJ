export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const KEY_STORAGE = "zc-api-key";

export function getKey(): string {
  if (typeof window === "undefined") return process.env.NEXT_PUBLIC_API_KEY || "zc-dev-user";
  return window.localStorage.getItem(KEY_STORAGE) || process.env.NEXT_PUBLIC_API_KEY || "zc-dev-user";
}

export function setKey(value: string) {
  window.localStorage.setItem(KEY_STORAGE, value.trim());
}

export function mediaUrl(path: string | null | undefined): string {
  if (!path) return "";
  if (path.startsWith("http")) return path;
  return authedUrl(path);
}

export function authedUrl(path: string): string {
  const url = new URL(`${API_BASE}${path}`);
  url.searchParams.set("k", getKey());
  return url.toString();
}

function authHeaders(extra?: HeadersInit): HeadersInit {
  return { ...(extra || {}), "X-API-Key": getKey() };
}

async function parse<T>(response: Response): Promise<T> {
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" ? detail : "请求失败";
    throw new Error(message);
  }
  return body as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store", headers: authHeaders() });
  return parse<T>(response);
}

export async function apiPost<T>(path: string, payload?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: authHeaders(payload ? { "Content-Type": "application/json" } : undefined),
    body: payload ? JSON.stringify(payload) : undefined,
  });
  return parse<T>(response);
}

export async function apiUpload<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE}${path}`, { method: "POST", body: form, headers: authHeaders() });
  return parse<T>(response);
}
