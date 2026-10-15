import { createChart, ColorType, type IChartApi, type ISeriesApi } from 'lightweight-charts';
import { useEffect, useRef } from 'react';

interface Props {
  data: { date: string; value: number }[];
  height?: number;
  color?: string;
}

/** 单序列面积图：大盘指数、回测净值都用它 */
export function AreaChart({ data, height = 200, color = '#4d9fff' }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  const hasData = data.length > 0;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const chart = createChart(host, {
      width: host.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#8b9bb4',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(35,44,61,0.35)' },
        horzLines: { color: 'rgba(35,44,61,0.35)' },
      },
      rightPriceScale: { borderColor: '#232c3d' },
      timeScale: { borderColor: '#232c3d' },
      handleScale: { axisPressedMouseMove: false },
    });
    seriesRef.current = chart.addAreaSeries({
      lineColor: color,
      topColor: `${color}44`,
      bottomColor: `${color}05`,
      lineWidth: 2,
      priceLineVisible: false,
    });
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.applyOptions({ width: host.clientWidth }));
    ro.observe(host);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [height, color, hasData]);

  useEffect(() => {
    if (!seriesRef.current || data.length === 0) return;
    seriesRef.current.setData(data.map((d) => ({ time: d.date, value: d.value })));
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  if (!hasData) return <div className="empty">暂无数据</div>;
  return <div ref={hostRef} className="chart" style={{ height }} />;
}
