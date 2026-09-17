// 实时曲线(星空版):uPlot 绘图,默认 legend 弃用——自定义通道芯片
// (色点 + key + 活值,点击开关);系列带向下渐隐的面积填充,星空感更柔。
import { useEffect, useRef, useState } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { store } from '../store';
import type { HttpConfig, PlotChannel } from '../types';

const WINDOW_S = 60;

function hexA(hex: string, a: number): string {
  const m = hex.replace('#', '');
  const r = parseInt(m.slice(0, 2), 16);
  const g = parseInt(m.slice(2, 4), 16);
  const b = parseInt(m.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

export default function ChartsTab({ config }: { config: HttpConfig | null }) {
  const channels = config?.plot_channels ?? [];
  const hostRef = useRef<HTMLDivElement>(null);
  const uRef = useRef<uPlot | null>(null);
  const dataRef = useRef<{ t: number[]; v: number[][] }>({ t: [], v: [] });
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const [live, setLive] = useState<Record<string, number | null>>({});

  useEffect(() => {
    setVisible(Object.fromEntries(channels.map((c) => [c.key, c.visible])));
  }, [config]);

  // 建/销毁图表
  useEffect(() => {
    const host = hostRef.current;
    if (!host || !channels.length) return;
    dataRef.current = { t: [], v: channels.map(() => []) };
    const axisStyle = {
      stroke: 'rgba(190,210,245,0.4)',
      font: '10px JetBrains Mono, monospace',
      grid: { stroke: 'rgba(140,170,255,0.06)', width: 1 },
      ticks: { stroke: 'rgba(140,170,255,0.15)', width: 1 },
      border: { show: false },
    };
    const u = new uPlot({
      width: host.clientWidth,
      height: host.clientHeight,
      scales: { x: { time: true } },
      series: [
        { label: 't' },
        ...channels.map((c): uPlot.Series => ({
          label: c.key,
          stroke: c.color,
          width: 1.8,
          points: { show: false },
          fill: (self: uPlot) => {
            const g = self.ctx.createLinearGradient(
              0, self.bbox.top, 0, self.bbox.top + self.bbox.height);
            g.addColorStop(0, hexA(c.color, 0.20));
            g.addColorStop(1, hexA(c.color, 0));
            return g;
          },
        })),
      ],
      axes: [axisStyle, axisStyle],
      legend: { show: false },
      cursor: { show: false },
      padding: [10, 8, 0, 0],
    }, [[], ...channels.map(() => [])] as uPlot.AlignedData, host);
    uRef.current = u;
    const ro = new ResizeObserver(() => {
      u.setSize({ width: host.clientWidth, height: host.clientHeight });
    });
    ro.observe(host);
    const timer = setInterval(() => {
      const d = dataRef.current;
      if (d.t.length > 1) u.setData([d.t, ...d.v] as uPlot.AlignedData);
      // 活值芯片
      const lv: Record<string, number | null> = {};
      channels.forEach((c, i) => {
        lv[c.key] = d.v[i].length ? d.v[i][d.v[i].length - 1] : null;
      });
      setLive(lv);
    }, 250);
    return () => { clearInterval(timer); ro.disconnect(); u.destroy(); uRef.current = null; };
  }, [channels]);

  // 可见性
  useEffect(() => {
    const u = uRef.current;
    if (!u) return;
    channels.forEach((c, i) => {
      if (c.key in visible) u.setSeries(i + 1, { show: visible[c.key] });
    });
  }, [visible, channels]);

  // SSE 订阅(不进 React state)
  useEffect(() => store.onTelemetry((f) => {
    const d = dataRef.current;
    if (!channels.length || !d.v.length) return;
    const has = channels.some((c) => typeof f.fields[c.key] === 'number');
    if (!has) return;
    const t = typeof f.ts === 'number' ? f.ts : Date.now() / 1000;
    d.t.push(t);
    channels.forEach((c, i) => {
      const v = f.fields[c.key];
      const prev = d.v[i].length ? d.v[i][d.v[i].length - 1] : 0;
      d.v[i].push(typeof v === 'number' ? v : prev);
    });
    const cutoff = t - WINDOW_S;
    let drop = 0;
    while (drop < d.t.length && d.t[drop] < cutoff) drop++;
    if (drop) { d.t.splice(0, drop); d.v.forEach((a) => a.splice(0, drop)); }
  }), [channels]);

  if (!channels.length) {
    return <div className="empty-hint">waiting /config plot_channels …</div>;
  }
  return (
    <div className="scope-wrap">
      <div className="scope-legend">
        {channels.map((c: PlotChannel) => {
          const on = !!visible[c.key];
          const v = live[c.key];
          return (
            <div
              key={c.key}
              className={`ch-chip ${on ? '' : 'off'}`}
              onClick={() => setVisible({ ...visible, [c.key]: !on })}
              title={on ? '点击隐藏' : '点击显示'}
            >
              <span className="c-dot" style={{ background: c.color, boxShadow: `0 0 6px ${c.color}` }} />
              <span className="c-key">{c.key}</span>
              <span className="c-val" style={{ color: on ? c.color : undefined }}>
                {v === null || v === undefined ? '--' : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>
      <div className="scope-host" ref={hostRef} />
    </div>
  );
}
