// XY 曲线渲染共用(canvas):自动范围、网格、发光折线、最新点高亮。
export interface XySeries { pts: [number, number][]; color: string; label?: string }

export function drawXy(canvas: HTMLCanvasElement, series: XySeries[]) {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth; const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const all = series.flatMap((s) => s.pts);
  if (!all.length) {
    ctx.fillStyle = 'rgba(140,170,210,0.35)';
    ctx.font = '12px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('no points', w / 2, h / 2);
    return;
  }
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  for (const [x, y] of all) {
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
  }
  const padX = Math.max((maxX - minX) * 0.06, 0.5);
  const padY = Math.max((maxY - minY) * 0.06, 0.5);
  minX -= padX; maxX += padX; minY -= padY; maxY += padY;
  const sx = (x: number) => ((x - minX) / (maxX - minX)) * (w - 20) + 10;
  // y 轴翻转(上 = +y)
  const sy = (y: number) => h - 10 - ((y - minY) / (maxY - minY)) * (h - 20);

  // 网格 + 原点十字
  ctx.strokeStyle = 'rgba(94,234,212,0.07)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 8; i++) {
    ctx.beginPath(); ctx.moveTo((w / 8) * i, 0); ctx.lineTo((w / 8) * i, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, (h / 8) * i); ctx.lineTo(w, (h / 8) * i); ctx.stroke();
  }
  if (minX < 0 && maxX > 0) {
    ctx.strokeStyle = 'rgba(94,234,212,0.2)';
    ctx.beginPath(); ctx.moveTo(sx(0), 0); ctx.lineTo(sx(0), h); ctx.stroke();
  }
  if (minY < 0 && maxY > 0) {
    ctx.strokeStyle = 'rgba(94,234,212,0.2)';
    ctx.beginPath(); ctx.moveTo(0, sy(0)); ctx.lineTo(w, sy(0)); ctx.stroke();
  }

  for (const s of series) {
    if (s.pts.length < 2) continue;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    ctx.shadowColor = s.color;
    ctx.shadowBlur = 6;
    ctx.beginPath();
    s.pts.forEach(([x, y], i) => {
      if (i === 0) ctx.moveTo(sx(x), sy(y)); else ctx.lineTo(sx(x), sy(y));
    });
    ctx.stroke();
    ctx.shadowBlur = 0;
    const [lx, ly] = s.pts[s.pts.length - 1];
    ctx.fillStyle = s.color;
    ctx.beginPath(); ctx.arc(sx(lx), sy(ly), 3.5, 0, Math.PI * 2); ctx.fill();
  }

  // 范围标注
  ctx.fillStyle = 'rgba(140,170,210,0.5)';
  ctx.font = '10px monospace';
  ctx.textAlign = 'left';
  ctx.fillText(`x [${minX.toFixed(1)}, ${maxX.toFixed(1)}]`, 8, h - 6);
  ctx.fillText(`y [${minY.toFixed(1)}, ${maxY.toFixed(1)}]`, 8, 12);
}
