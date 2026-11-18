/** A tiny line chart of a watch's recent values. Pure SVG, no chart library. */
export function Sparkline({
  values,
  width = 120,
  height = 28,
  threshold,
}: {
  values: number[];
  width?: number;
  height?: number;
  threshold?: number;
}) {
  if (values.length === 0) {
    return <span className="text-muted-foreground text-xs">no data yet</span>;
  }
  const all = threshold === undefined ? values : [...values, threshold];
  const min = Math.min(...all);
  const max = Math.max(...all);
  const span = max - min || 1;
  const pad = 2;
  const x = (i: number) =>
    values.length === 1
      ? width / 2
      : pad + (i * (width - 2 * pad)) / (values.length - 1);
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - 2 * pad);
  const points = values
    .map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  const last = values[values.length - 1];

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Recent values, latest ${last}`}
      className="overflow-visible text-primary"
    >
      {threshold !== undefined && (
        <line
          x1={0}
          x2={width}
          y1={y(threshold)}
          y2={y(threshold)}
          className="stroke-amber-500"
          strokeDasharray="3 3"
          strokeWidth={1}
        />
      )}
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
      />
      <circle
        cx={x(values.length - 1)}
        cy={y(last)}
        r={2.5}
        fill="currentColor"
      />
    </svg>
  );
}
