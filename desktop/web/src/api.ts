// 桥 HTTP API 封装(相对路径,开发与 exe 托管同构)
import type {
  BatchResponse, ControlSchema, HttpConfig, Proposal, Snapshot,
} from './types';

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export const api = {
  snapshot: () => getJson<Snapshot>('/snapshot'),
  schema: () => getJson<ControlSchema>('/schema'),
  config: () => getJson<HttpConfig>('/config'),
  trajectory: (since = 0) =>
    getJson<{ points: Record<string, unknown>[]; count: number; since: number }>(
      `/trajectory?since=${since}`),
  pathsList: () =>
    getJson<Record<string, { filename: string; size_kb: number; mtime: string }[]>>(
      '/paths/list'),
  pathsLoad: (dir: string, file: string) =>
    getJson<unknown>(`/paths/load?dir=${encodeURIComponent(dir)}&file=${encodeURIComponent(file)}`),
  proposalList: () => getJson<{ proposals: Proposal[] }>('/proposal/list'),
};

/** P2 建议-确认流:人确认(apply)/拒绝(reject)一条提案 */
export async function decideProposal(
  id: number, decision: 'apply' | 'reject',
): Promise<Proposal> {
  const r = await fetch('/proposal/decide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, decision }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.error ?? `/proposal/decide -> HTTP ${r.status}`);
  }
  return r.json() as Promise<Proposal>;
}

/** /batch 失败时抛出;携带 HTTP status 与护栏 violations(400 拒绝时),
 *  让调用方能就地展示拒绝原因而不是一句 "HTTP 400"。 */
export class BatchError extends Error {
  status?: number;
  violations?: { rule?: string; param?: string; detail?: string }[];
}

export async function postBatch(
  commands: { cmd: string; expect?: string; timeout_ms?: number }[],
  stopOnError = false,
): Promise<BatchResponse> {
  const r = await fetch('/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      commands, stop_on_error: stopOnError, role: 'agent',
    }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({} as Record<string, unknown>));
    const err = new BatchError(
      r.status === 409
        ? 'another /batch in progress (409)'
        : (body.error as string) ?? `/batch -> HTTP ${r.status}`);
    err.status = r.status;
    if (Array.isArray(body.violations)) {
      err.violations = body.violations as BatchError['violations'];
    }
    throw err;
  }
  return r.json() as Promise<BatchResponse>;
}
