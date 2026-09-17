// CONSOLE(星空版):日志 + 输入框;快捷命令收纳进搜索弹层(按命令首词分组,
// 含 (!) 标危险的红色),不再铺一排按钮。
import { useEffect, useMemo, useRef, useState } from 'react';
import { postBatch } from '../api';
import { store, useStore } from '../store';
import type { HttpConfig, QuickCommand } from '../types';

export default function Console({ config }: { config: HttpConfig | null }) {
  const { console: entries } = useStore();
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [quickOpen, setQuickOpen] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries]);

  async function send(cmd: string) {
    const c = cmd.trim();
    if (!c || busy) return;
    setBusy(true);
    try {
      const r = await postBatch([{ cmd: c, expect: 'ACK' }]);
      const res = r.results[0];
      store.log('ui', `→ ${res?.status ?? '??'}${res?.elapsed_ms != null ? ` (${res.elapsed_ms}ms)` : ''}`);
    } catch (e) {
      store.log('ui', `→ batch error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="console-head">
        <span className="c-title">✦ CONSOLE</span>
        <span className="spacer" />
        <button className="btn ghost" onClick={() => setQuickOpen(!quickOpen)}>
          {quickOpen ? '收起命令 ▴' : '快捷命令 ☰'}
        </button>
      </div>
      <div className="console-body">
        <div className="console-log" ref={logRef}>
          {entries.map((e, i) => (
            <div key={i} className={e.kind}>
              <span className="ts">{new Date(e.ts * 1000).toLocaleTimeString('en-GB')}</span>
              {e.text}
            </div>
          ))}
        </div>
      </div>
      <div className="console-input" style={{ position: 'relative' }}>
        <input
          value={input}
          placeholder="command… (Enter 发送,走 /batch expect=ACK)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { send(input); setInput(''); } }}
        />
        <button className="btn" disabled={busy || !input.trim()}
                onClick={() => { send(input); setInput(''); }}>
          SEND
        </button>
        {quickOpen && (
          <QuickPopup
            commands={config?.quick_commands ?? []}
            onPick={(cmd) => { setQuickOpen(false); send(cmd); }}
            onClose={() => setQuickOpen(false)}
          />
        )}
      </div>
    </>
  );
}

function QuickPopup({ commands, onPick, onClose }: {
  commands: QuickCommand[]; onPick: (cmd: string) => void; onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const filtered = commands.filter((c) =>
      !needle || c.label.toLowerCase().includes(needle)
      || c.command.toLowerCase().includes(needle));
    // 按命令首词分组(GET/START/STOP/…),纯语法分组无领域知识
    const m = new Map<string, QuickCommand[]>();
    for (const c of filtered) {
      const head = c.command.split(/\s+/)[0] ?? '?';
      if (!m.has(head)) m.set(head, []);
      m.get(head)!.push(c);
    }
    return [...m.entries()];
  }, [commands, q]);

  return (
    <div className="quick-pop">
      <div className="qp-search">
        <input autoFocus placeholder={`搜索 ${commands.length} 条快捷命令… (Esc 关闭)`}
               value={q} onChange={(e) => setQ(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }} />
      </div>
      <div className="qp-list">
        {groups.map(([head, items]) => (
          <div key={head}>
            <div className="qp-group">{head} ({items.length})</div>
            {items.map((c) => {
              const danger = c.label.includes('(!)');
              return (
                <div key={c.label} className={`qp-item ${danger ? 'danger' : ''}`}
                     onClick={() => onPick(c.command)}>
                  <span>{c.label}</span>
                  <span className="qp-cmd">{c.command}</span>
                </div>
              );
            })}
          </div>
        ))}
        {!groups.length && <div className="qp-group">no match</div>}
      </div>
    </div>
  );
}
