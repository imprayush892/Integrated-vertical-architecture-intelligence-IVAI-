// JSON-RPC client for the local engine (same origin; token from /api/token).
let token: string | null = null;

export class RpcError extends Error {
  constructor(message: string, public code: number, public data?: unknown) { super(message); }
}

export async function getToken(): Promise<string> {
  if (token) return token;
  const r = await fetch("/api/token");
  if (!r.ok) throw new Error("The planner engine is not reachable.");
  token = (await r.json()).token as string;
  return token;
}

let seq = 0;
export async function rpc<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  const t = await getToken();
  const r = await fetch("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Planner-Token": t },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++seq, method, params }),
  });
  if (r.status === 401) { token = null; throw new RpcError("Session expired - reload the app.", 401); }
  const body = await r.json();
  if (body.error) throw new RpcError(body.error.message, body.error.code, body.error.data);
  return body.result as T;
}

export function subscribe(onEvent: (ev: Record<string, unknown>) => void): () => void {
  let es: EventSource | null = null;
  let closed = false;
  let retry = 1000;
  const open = async () => {
    const t = await getToken();
    if (closed) return;
    es = new EventSource(`/api/events?t=${encodeURIComponent(t)}`);
    es.onmessage = (m) => { retry = 1000; try { onEvent(JSON.parse(m.data)); } catch { /* ignore */ } };
    es.onerror = () => { es?.close(); if (!closed) setTimeout(open, (retry = Math.min(retry * 2, 15000))); };
  };
  void open();
  return () => { closed = true; es?.close(); };
}

export async function uploadDrawing(file: File, purpose: "check" | "plate" = "check", tower = "T1"): Promise<unknown> {
  const t = await getToken();
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`/api/upload/drawing?purpose=${purpose}&tower=${encodeURIComponent(tower)}`, { method: "POST", headers: { "X-Planner-Token": t }, body: fd });
  const body = await r.json();
  if (!r.ok) throw new RpcError(body.detail ?? "Upload failed", r.status);
  return body;
}

export async function dxfUrl(tower = "T1"): Promise<string> {
  return `/api/download/dxf?t=${encodeURIComponent(await getToken())}&tower=${encodeURIComponent(tower)}`;
}
