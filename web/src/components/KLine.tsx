import { createChart, ColorType, CrosshairMode, type IChartApi, type ISeriesApi } from 'lightweight-charts';
import { useEffect, useRef } from 'react';
import type { KlineRow } from '../lib/api';

interface Props {
  data: KlineRow[];
  height?: number;
  /** 叠加均线（5/10/30/60），和你在 smis/SteamDT 上看到的口径一致 */
  ma?: boolean;
}

export function KLine({ data, height = 380, ma = true }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const maRefs = useRef<ISeriesApi<'Line'>[]>([]);

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
        vertLines: { color: 'rgba(35,44,61,0.45)' },
        horzLines: { color: 'rgba(35,44,61,0.45)' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#232c3d' },
      leftPriceScale: { visible: false },
      timeScale: { borderColor: '#232c3d', rightOffset: 4 },
    });

    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });
    candleRef.current = chart.addCandlestickSeries({
      upColor: '#2ecc8f',
      downColor: '#ff5c5c',
      wickUpColor: '#2ecc8f',
      wickDownColor: '#ff5c5c',
      borderVisible: false,
    });
    volumeRef.current = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });

    maRefs.current = [];
    if (ma) {
      for (const color of ['#4d9fff', '#f5a524', '#8b5cf6', '#ff7ab6']) {
        maRefs.current.push(
          chart.addLineSeries({
            color,
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
          }),
        );
      }
    }

    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.applyOptions({ width: host.clientWidth }));
    ro.observe(host);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      maRefs.current = [];
    };
  }, [height, ma]);

  useEffect(() => {
    if (!candleRef.current || data.length === 0) return;
    candleRef.current.setData(
      data.map((d) => ({ time: d.date, open: d.o, high: d.h, low: d.l, close: d.c })),
    );
    volumeRef.current?.setData(
      data
        .filter((d) => d.v !== null)
        .map((d) => ({
          time: d.date,
          value: d.v as number,
          color: d.c >= d.o ? 'rgba(46,204,143,0.45)' : 'rgba(255,92,92,0.45)',
        })),
    );
    if (maRefs.current.length === 4) {
      const periods = [5, 10, 30, 60];
      periods.forEach((p, idx) => {
        const out: { time: string; value: number }[] = [];
        let sum = 0;
        for (let i = 0; i < data.length; i++) {
          sum += data[i].c;
          if (i >= p) sum -= data[i - p].c;
          if (i >= p - 1) out.push({ time: data[i].date, value: sum / p });
        }
        maRefs.current[idx]?.setData(out);
      });
    }
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  if (data.length === 0) return <div className="empty">暂无 K 线数据（管线还没推送 kline）</div>;
  return <div ref={hostRef} className="chart" style={{ height }} />;
}
