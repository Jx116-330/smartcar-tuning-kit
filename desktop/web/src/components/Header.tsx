import type { ReactNode } from 'react';
import type { HttpConfig, Snapshot } from '../types';
import { useStore } from '../store';

interface Props {
  snapshot: Snapshot | null;
  config: HttpConfig | null;
  error: string | null;
  actions?: ReactNode;   // 安全动作插槽(SafetyActions,声明驱动)
}

/** HEADER:链路状态 · 桥/agent 状态 · schema_hash · SSE 帧率(全部数据驱动) */
export default function Header({ snapshot, config, error, actions }: Props) {
  const { link, connected, framesPerSec } = useStore();
  const effLink = link ?? snapshot?.link ?? null;
  const car = effLink && effLink.running && effLink.connections > 0;
  const dotCls = error ? 'bad' : car ? 'ok' : connected ? 'warn' : 'bad';
  const hash = snapshot?.bridge?.schema_hash;

  return (
    <header className="hud-header">
      <span className="title glow-cyan">{config?.app_title ?? 'TUNING CONSOLE'}</span>
      <span className="stat">
        <span className={`dot ${dotCls}`} />
        {error
          ? <b>{error}</b>
          : <b>{car ? `LINK ${effLink!.mode}:${effLink!.connections}` : connected ? 'NO CAR' : 'BRIDGE DOWN'}</b>}
      </span>
      <span className="stat">hb_age <b>{effLink?.hb_age_ms ?? '--'}ms</b></span>
      <span className="stat">sse <b>{connected ? `${framesPerSec.toFixed(0)}f/s` : 'off'}</b></span>
      <span className="stat">schema <b>{hash ? hash.slice(0, 12) : '--'}</b></span>
      <span className="stat">bridge <b>{snapshot?.bridge?.version ?? '--'}</b></span>
      <span className="stat">uptime <b>{snapshot?.bridge?.uptime_s ?? '--'}s</b></span>
      {actions}
    </header>
  );
}
