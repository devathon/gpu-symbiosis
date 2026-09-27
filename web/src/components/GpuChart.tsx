import { useEffect, useRef } from "react";
import * as echarts from "echarts";

interface Sample {
  t: number;
  vram: number;
  gpu: number;
  temp: number | null;
}

interface Props {
  samples: Sample[];
}

/**
 * Streaming line chart.
 *
 * The full option (axes, legend, series shells) is set once at mount.
 * Each tick updates ONLY the series data via a minimal setOption —
 * ECharts diffs the data in place and redraws the lines. No replaceMerge
 * (which destroys/recreates the series and caused the "whole graph
 * refreshing" flicker) and animation is off, so updates are smooth.
 *
 * Note: chart.appendData was tried first, but in this ECharts version it
 * updates the data store (tooltips work) without rendering the line path
 * on a time axis — the lines stayed invisible. setOption is the reliable
 * path here.
 *
 * Temperature is on a second (right-hand) axis: it is a °C value, not a
 * percentage, so it cannot share the 0–100 % scale.
 */
export default function GpuChart({ samples }: Props) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!elRef.current) return;
    const chart = echarts.init(elRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    (window as unknown as Record<string, unknown>).__gpuChart = chart; // dev hook
    chart.setOption({
      backgroundColor: "transparent",
      animation: false,
      tooltip: {
        trigger: "axis",
        formatter: (params: unknown) => {
          const arr = (Array.isArray(params) ? params : [params]) as Array<{
            axisValueLabel?: string;
            marker?: string;
            seriesName?: string;
            value?: unknown;
          }>;
          if (arr.length === 0) return "";
          let out = arr[0].axisValueLabel ?? "";
          for (const p of arr) {
            const v = Array.isArray(p.value) ? (p.value[1] as number) : (p.value as number);
            if (v == null) continue;
            const unit = p.seriesName === "Temp" ? "°C" : "%";
            out += `<br/>${p.marker ?? ""}${p.seriesName ?? ""}: ${Number(v).toFixed(1)} ${unit}`;
          }
          return out;
        },
      },
      grid: { left: 48, right: 44, top: 28, bottom: 24 },
      legend: { textStyle: { color: "#a1a1aa" }, data: ["VRAM", "GPU", "Temp"] },
      // Time axis: x derived directly from the [timestamp, value] pairs.
      xAxis: {
        type: "time",
        axisLabel: { color: "#71717a", formatter: "{HH}:{mm}:{ss}" },
      },
      yAxis: [
        {
          type: "value",
          min: 0,
          max: 100,
          axisLabel: { color: "#71717a", formatter: (v: number) => `${v}%` },
          splitLine: { lineStyle: { color: "#27272a" } },
        },
        {
          type: "value",
          yAxisIndex: 1,
          position: "right",
          min: 0,
          max: 100,
          axisLabel: { color: "#fb7185", formatter: (v: number) => `${v}°C` },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: "VRAM",
          type: "line",
          data: [],
          smooth: true,
          showSymbol: false,
          lineStyle: { color: "#f59e0b", width: 2.5 },
          areaStyle: { color: "rgba(245,158,11,0.08)" },
        },
        {
          name: "GPU",
          type: "line",
          data: [],
          smooth: true,
          showSymbol: false,
          lineStyle: { color: "#34d399", width: 2.5 },
        },
        {
          name: "Temp",
          type: "line",
          data: [],
          smooth: true,
          showSymbol: false,
          yAxisIndex: 1,
          lineStyle: { color: "#fb7185", width: 1.5, type: "dashed" },
        },
      ],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, []);

  // Each tick: update only the series data (ECharts diffs in place).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || samples.length === 0) return;
    chart.setOption({
      series: [
        { name: "VRAM", data: samples.map((s) => [s.t, s.vram]) },
        { name: "GPU", data: samples.map((s) => [s.t, s.gpu]) },
        { name: "Temp", data: samples.map((s) => [s.t, s.temp]) },
      ],
    });
  }, [samples]);

  return <div ref={elRef} className="h-56 w-full" />;
}
