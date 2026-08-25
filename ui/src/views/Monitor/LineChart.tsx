import { Card } from "../../components/Card";
import kpi from "../Metrics/MetricsView.module.css";
import { LABEL_Y, VIEW, lineSeries } from "./charts";
import type { Sample } from "./types";

interface Props {
  title: string;
  sub: string;
  samples: Sample[];
  window: string;
  words: (value: number) => string;
  label: string;
}

/** One unit, one line, KPI chart rules: nulls break, last bucket is partial. */
export function LineChart({ title, sub, samples, window, words, label }: Props) {
  const { series, grid, ticks } = lineSeries(samples, window, words);
  const measured = series.points.length > 0;

  return (
    <Card className={kpi.chart}>
      <span className={kpi.chartTitle}>{title}</span>
      <span className={kpi.chartSub}>{sub}</span>
      {measured ? (
        <svg
          viewBox={`0 0 ${VIEW.width} ${VIEW.height}`}
          width="100%"
          role="img"
          aria-label={label}
        >
          {grid.map((line) => (
            <g key={line.label}>
              <line x1={30} y1={line.y} x2={434} y2={line.y} className={kpi.gridline} />
              <text x={22} y={line.y + 3.5} textAnchor="end" className={kpi.axisLabel}>
                {line.label}
              </text>
            </g>
          ))}
          {ticks.map((tick) => (
            <text
              key={`${tick.x}-${tick.label}`}
              x={tick.x}
              y={LABEL_Y}
              textAnchor="middle"
              className={kpi.axisLabel}
            >
              {tick.label}
            </text>
          ))}
          {series.segments.map((d) => (
            <path
              key={d}
              d={d}
              fill="none"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              className={kpi.line1}
            />
          ))}
          {series.points.map((point) => (
            <circle key={point.key} cx={point.x} cy={point.y} r={3.5} className={kpi.dot1}>
              <title>{point.title}</title>
            </circle>
          ))}
        </svg>
      ) : (
        <p className={kpi.chartEmpty}>Nothing measured in this window yet.</p>
      )}
    </Card>
  );
}
