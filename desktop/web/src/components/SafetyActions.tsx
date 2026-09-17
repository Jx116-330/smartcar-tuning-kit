// 安全动作(声明驱动,前端零领域知识):schema.actions 声明 + protocol.commands
// 模板名引用。提供 Header 常驻按钮 + active_when 命中时悬浮大按钮 + 快捷键
// (输入焦点豁免) + 失败按声明重试、最终失败常驻红横幅。
// 模板解析不到或含不可填占位 → 该动作不渲染(schema 无 actions 键时整体消失)。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ActionDecl, ControlSchema, TelemetryFrame } from '../types';
import { postBatch } from '../api';
import { store } from '../store';

interface ResolvedAction {
  decl: ActionDecl;
  wire: string;   // 展开后的线上命令串
}

function resolveActions(schema: ControlSchema): ResolvedAction[] {
  const cmds = schema.protocol?.commands ?? [];
  const out: ResolvedAction[] = [];
  for (const decl of schema.actions ?? []) {
    if (!decl.id || !decl.command) continue;
    const tpl = cmds.find(([n]) => n === decl.command)?.[1];
    if (!tpl || /<[^>]+>/.test(tpl)) continue;   // 模板缺失/需填参 → 不渲染
    out.push({ decl, wire: tpl });
  }
  return out;
}

/** active_when 求值:帧字段值若按 telemetry 声明的 unit 位命中枚举表,
 *  先映射成枚举名再与 in 列表比(字符串化比较,数值/名称皆可声明)。 */
function evalActive(
  schema: ControlSchema, decl: ActionDecl, frame: TelemetryFrame,
): boolean {
  const aw = decl.active_when;
  if (!aw || frame.type !== aw.stream) return false;
  let v = frame.fields[aw.field];
  if (v === undefined || v === null) return false;
  const unit = schema.telemetry?.[aw.stream]
    ?.find(([f]) => f === aw.field)?.[1];
  const enumDef = unit ? schema.enums?.[unit] : undefined;
  if (enumDef) v = enumDef[String(v)] ?? v;
  return aw.in.some((x) => String(x) === String(v));
}

const labelOf = (a: ResolvedAction) => a.decl.label ?? a.decl.id;

export default function SafetyActions({ schema }: { schema: ControlSchema | null }) {
  const actions = useMemo(
    () => (schema ? resolveActions(schema) : []), [schema]);
  const [active, setActive] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [failed, setFailed] = useState<Record<string, string>>({});
  const busyRef = useRef(busy);
  busyRef.current = busy;

  // active_when 求值(遥测帧驱动;无声明的动作恒非 active)
  useEffect(() => {
    if (!schema || actions.length === 0) return;
    return store.onTelemetry((frame) => {
      setActive((prev) => {
        let changed = false;
        const next = { ...prev };
        for (const a of actions) {
          if (!a.decl.active_when) continue;
          const v = evalActive(schema, a.decl, frame);
          if (next[a.decl.id] !== v) { next[a.decl.id] = v; changed = true; }
        }
        return changed ? next : prev;
      });
    });
  }, [schema, actions]);

  // 发送:expect=ACK 经 /batch;失败按 retry 声明重试(409 互斥/网络错误同属
  // 可重试);全部失败 → 常驻横幅,直到下次成功或人工关闭
  const fire = useCallback(async (a: ResolvedAction) => {
    const id = a.decl.id;
    if (busyRef.current[id]) return;
    setBusy((p) => ({ ...p, [id]: true }));
    const attempts = Math.max(1, a.decl.retry?.count ?? 1);
    const interval = a.decl.retry?.interval_ms ?? 200;
    let ok = false;
    let lastMsg = 'no result';
    for (let i = 0; i < attempts && !ok; i++) {
      if (i > 0) await new Promise((r) => setTimeout(r, interval));
      try {
        const r = await postBatch(
          [{ cmd: a.wire, expect: 'ACK', timeout_ms: 1500 }]);
        const res = r.results[0];
        ok = res?.status === 'ack';
        lastMsg = res?.status ?? 'no result';
      } catch (e) {
        lastMsg = (e as Error).message;
      }
    }
    setBusy((p) => ({ ...p, [id]: false }));
    if (ok) {
      setFailed((p) => { const n = { ...p }; delete n[id]; return n; });
      store.log('ui', `[安全动作] ${labelOf(a)} -> ack`);
    } else {
      setFailed((p) => ({ ...p, [id]: lastMsg }));
      store.log('ui', `⛔ [安全动作] ${labelOf(a)} 未确认(${attempts}次): ${lastMsg}`);
    }
  }, []);

  const fireRef = useRef(fire);
  fireRef.current = fire;

  // 快捷键:全局 keydown,焦点在输入控件时豁免
  useEffect(() => {
    if (actions.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT'
                || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      for (const a of actions) {
        const hit = (a.decl.hotkeys ?? []).some((k) =>
          (k === 'Space' && (e.code === 'Space' || e.key === ' '))
          || (k === 'Escape' && e.key === 'Escape')
          || e.key === k);
        if (hit) {
          e.preventDefault();
          void fireRef.current(a);
          return;
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [actions]);

  if (actions.length === 0) return null;

  const hotkeyHint = (a: ResolvedAction) =>
    (a.decl.hotkeys?.length ?? 0) > 0
      ? `快捷键: ${a.decl.hotkeys!.join(' / ')}`
      : undefined;

  return (
    <>
      <span className="safety-group">
        {actions.map((a) => (
          <button
            key={a.decl.id}
            className={`btn safety-btn${a.decl.danger ? ' danger' : ''}${active[a.decl.id] ? ' armed' : ''}`}
            disabled={!!busy[a.decl.id]}
            title={hotkeyHint(a)}
            onClick={() => void fire(a)}
          >{busy[a.decl.id] ? '…' : labelOf(a)}</button>
        ))}
      </span>
      {actions
        .filter((a) => a.decl.danger && active[a.decl.id])
        .map((a) => (
          <button
            key={`float-${a.decl.id}`}
            className="safety-float"
            disabled={!!busy[a.decl.id]}
            title={hotkeyHint(a)}
            onClick={() => void fire(a)}
          >■ {labelOf(a)}</button>
        ))}
      {Object.entries(failed).map(([id, msg]) => {
        const a = actions.find((x) => x.decl.id === id);
        return (
          <div key={`fail-${id}`} className="safety-banner">
            <span>⛔ {a ? labelOf(a) : id} 未确认 —— 立即人工介入（{msg}）</span>
            <button
              title="关闭(仅消横幅,不代表动作成功)"
              onClick={() => setFailed((p) => {
                const n = { ...p }; delete n[id]; return n;
              })}
            >✕</button>
          </div>
        );
      })}
    </>
  );
}
