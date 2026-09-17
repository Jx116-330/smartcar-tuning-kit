// SSE 实时数据 store:EventSource 单连接,订阅者模式(不用外部状态库)。
import { useSyncExternalStore } from 'react';
import type { ConsoleEntry, LinkState, Proposal, TelemetryFrame } from './types';

export interface StoreState {
  link: LinkState | null;
  connected: boolean;          // SSE 连接状态(前端到桥)
  latestByType: Record<string, Record<string, unknown>>;
  console: ConsoleEntry[];
  framesPerSec: number;
  lastFrameAt: number;
  proposals: Proposal[];       // P2 建议-确认流
}

const MAX_CONSOLE = 500;

type TelemetryListener = (f: TelemetryFrame) => void;

class Store {
  state: StoreState = {
    link: null,
    connected: false,
    latestByType: {},
    console: [],
    framesPerSec: 0,
    lastFrameAt: 0,
    proposals: [],
  };

  private listeners = new Set<() => void>();
  private telemetryListeners = new Set<TelemetryListener>();
  private es: EventSource | null = null;
  private frameTimes: number[] = [];

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    this.start();
    return () => { this.listeners.delete(fn); };
  };

  getSnapshot = () => this.state;

  /** 遥测帧原始订阅(曲线/轨迹用,不进 React state 避免每帧重渲染) */
  onTelemetry = (fn: TelemetryListener) => {
    this.telemetryListeners.add(fn);
    this.start();
    return () => { this.telemetryListeners.delete(fn); };
  };

  private emit() {
    this.state = { ...this.state };
    this.listeners.forEach((fn) => fn());
  }

  private pushConsole(kind: ConsoleEntry['kind'], text: string) {
    const entry = { kind, text, ts: Date.now() / 1000 };
    const next = [...this.state.console, entry];
    this.state = { ...this.state, console: next.slice(-MAX_CONSOLE) };
    this.listeners.forEach((fn) => fn());
  }

  log(kind: ConsoleEntry['kind'], text: string) {
    this.pushConsole(kind, text);
  }

  private start() {
    if (this.es) return;
    const es = new EventSource('/events');
    this.es = es;
    es.onopen = () => {
      this.state = { ...this.state, connected: true };
      this.listeners.forEach((fn) => fn());
    };
    es.onerror = () => {
      this.state = { ...this.state, connected: false };
      this.listeners.forEach((fn) => fn());
      // EventSource 自带重连;浏本断开由桥侧清理
    };
    es.addEventListener('link', (ev) => {
      try {
        this.state = { ...this.state, link: JSON.parse((ev as MessageEvent).data) };
        this.listeners.forEach((fn) => fn());
      } catch { /* ignore */ }
    });
    es.addEventListener('cmd', (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data);
        this.pushConsole('cmd', `> ${d.cmd}`);
      } catch { /* ignore */ }
    });
    es.addEventListener('response', (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data);
        const f = d.fields as Record<string, unknown>;
        const summary = Object.entries(f)
          .filter(([k]) => !k.startsWith('_'))
          .map(([k, v]) => `${k}=${v}`)
          .join(' ');
        this.pushConsole('response', `${d.ptype} ${summary}`);
      } catch { /* ignore */ }
    });
    // P2 建议-确认流:提案创建/状态变更都走同一事件,按 id upsert
    es.addEventListener('proposal', (ev) => {
      try {
        const p = JSON.parse((ev as MessageEvent).data) as Proposal;
        const rest = this.state.proposals.filter((x) => x.id !== p.id);
        this.state = { ...this.state, proposals: [...rest, p] };
        this.listeners.forEach((fn) => fn());
        if (p.status === 'pending') {
          this.pushConsole('ui', `[提案#${p.id}] agent 提议 ${p.commands.length} 条参数变更,待人确认`);
        }
      } catch { /* ignore */ }
    });
    // 启动时拉一次存量提案(桥重启即清,与内存态一致)
    fetch('/proposal/list')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.proposals) {
          this.state = { ...this.state, proposals: d.proposals };
          this.listeners.forEach((fn) => fn());
        }
      })
      .catch(() => { /* 桥版本无此端点时静默 */ });
    es.onmessage = (ev) => {
      let frame: TelemetryFrame;
      try {
        frame = JSON.parse(ev.data);
      } catch { return; }
      const now = Date.now();
      this.frameTimes.push(now);
      while (this.frameTimes.length && this.frameTimes[0] < now - 2000) {
        this.frameTimes.shift();
      }
      this.state.latestByType[frame.type] = frame.fields;
      this.state.lastFrameAt = now;
      this.telemetryListeners.forEach((fn) => fn(frame));
    };
    // 帧率每秒刷一次 React state
    setInterval(() => {
      const fps = this.frameTimes.length / 2;
      if (Math.abs(fps - this.state.framesPerSec) >= 0.5) {
        this.state = { ...this.state, framesPerSec: fps };
        this.listeners.forEach((fn) => fn());
      }
    }, 1000);
  }
}

export const store = new Store();

export function useStore() {
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}
