// 从任意字段 dict 嗅探 (x, y) 数值对:先精确 x/y,再 *px/*py(取先出现的
// = current 位),最后 *x/*y。形状嗅探,不绑定具体字段名。
export function findXY(o: Record<string, unknown>): [number, number] | null {
  const num = (k: string) => (typeof o[k] === 'number' ? (o[k] as number) : null);
  if (num('x') !== null && num('y') !== null) return [num('x')!, num('y')!];
  const keys = Object.keys(o);
  const xk = keys.find((k) => /px$/i.test(k)) ?? keys.find((k) => /(^|_)x$/i.test(k));
  const yk = keys.find((k) => /py$/i.test(k)) ?? keys.find((k) => /(^|_)y$/i.test(k));
  if (xk && yk && num(xk) !== null && num(yk) !== null) return [num(xk)!, num(yk)!];
  return null;
}
