import { useEffect, useMemo, useRef } from 'react'
import ReactECharts from 'echarts-for-react'
import * as echarts from 'echarts'
import type { ValueIndicators } from '../../api/types'

/** K线下方的选股指标曲线: PE(TTM) / 滚动一年股息率 / 距120日线比例(bias)。
 *  三行子图共享日期轴与 dataZoom, 通过 echarts.connect 与上方 K 线联动缩放;
 *  虚线标出策略阈值: PE≤20 · 股息率>3% · bias 0.88(买)/0.90(侧门)/1.08(离场)。 */
export default function ValueIndicatorsChart({
  ind,
  group,
  range,
  height = 300,
}: {
  ind: ValueIndicators
  group: string
  range: { start?: string; end?: string }
  height?: number
}) {
  const chartRef = useRef<any>(null)
  const dates = ind.dates

  const option = useMemo(() => {
    const refLine = (yAxisIndex: number, yAxis: number, label: string, color: string) => ({
      silent: true,
      symbol: 'none',
      yAxisIndex,
      lineStyle: { color, type: 'dashed', width: 1 },
      label: { formatter: label, color, fontSize: 9, position: 'insideEndTop' },
      data: [{ yAxis }],
    })
    const axisBase = {
      axisLine: { lineStyle: { color: '#374151' } },
      axisLabel: { color: '#9ca3af', fontSize: 9 },
    }
    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(17,24,39,0.95)',
        borderColor: '#374151',
        textStyle: { color: '#e5e7eb', fontSize: 11 },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 48, right: 16, top: 16, height: 64 },
        { left: 48, right: 16, top: 104, height: 64 },
        { left: 48, right: 16, top: 192, height: 64 },
      ],
      xAxis: [0, 1, 2].map(i => ({
        type: 'category',
        gridIndex: i,
        data: dates,
        ...axisBase,
        axisLabel: i === 2 ? axisBase.axisLabel : { show: false },
      })),
      yAxis: [
        { gridIndex: 0, scale: true, name: 'PE', nameTextStyle: { color: '#9ca3af', fontSize: 9 }, splitLine: { lineStyle: { color: '#1f2937' } }, axisLabel: { color: '#9ca3af', fontSize: 9 } },
        { gridIndex: 1, scale: true, name: '股息率%', nameTextStyle: { color: '#9ca3af', fontSize: 9 }, splitLine: { lineStyle: { color: '#1f2937' } }, axisLabel: { color: '#9ca3af', fontSize: 9 } },
        { gridIndex: 2, scale: true, name: 'bias', nameTextStyle: { color: '#9ca3af', fontSize: 9 }, splitLine: { lineStyle: { color: '#1f2937' } }, axisLabel: { color: '#9ca3af', fontSize: 9 } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1, 2], start: 70, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1, 2], height: 14, bottom: 4, borderColor: '#374151', textStyle: { color: '#9ca3af', fontSize: 9 } },
      ],
      series: [
        {
          name: 'PE(TTM)',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: ind.pe,
          showSymbol: false,
          connectNulls: false,
          lineStyle: { color: '#a78bfa', width: 1 },
          markLine: refLine(0, 20, 'PE=20 上限', '#6b7280'),
        },
        {
          name: '滚动股息率',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: ind.dy,
          showSymbol: false,
          lineStyle: { color: '#34d399', width: 1 },
          markLine: refLine(1, 3, '3% 门槛', '#6b7280'),
        },
        {
          name: '距120日线',
          type: 'line',
          xAxisIndex: 2,
          yAxisIndex: 2,
          data: ind.bias,
          showSymbol: false,
          lineStyle: { color: '#38bdf8', width: 1 },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [
              { yAxis: 0.88, lineStyle: { color: '#22c55e', type: 'dashed', width: 1 }, label: { formatter: '0.88 主买点', color: '#22c55e', fontSize: 9 } },
              { yAxis: 0.9, lineStyle: { color: '#f59e0b', type: 'dashed', width: 1 }, label: { formatter: '0.90 侧门', color: '#f59e0b', fontSize: 9 } },
              { yAxis: 1.08, lineStyle: { color: '#ef4444', type: 'dashed', width: 1 }, label: { formatter: '1.08 离场', color: '#ef4444', fontSize: 9 } },
            ],
          },
        },
      ],
    }
  }, [ind, dates])

  // 时间范围 → 百分比缩放(与 K 线各自日期轴对齐)
  useEffect(() => {
    const inst = chartRef.current?.getEchartsInstance?.()
    if (!inst || !dates.length) return
    const pct = (d?: string, isEnd = false) => {
      if (!d) return isEnd ? 100 : 0
      let idx: number
      if (isEnd) {
        idx = -1
        for (let i = dates.length - 1; i >= 0; i--) {
          if (dates[i] <= d) { idx = i; break }
        }
        if (idx < 0) idx = 0
      } else {
        idx = dates.findIndex(x => x >= d)
        if (idx < 0) idx = dates.length - 1
      }
      return Math.max(0, Math.min(100, (idx / (dates.length - 1)) * 100))
    }
    inst.dispatchAction({ type: 'dataZoom', start: pct(range.start), end: pct(range.end, true) }, { silent: true })
  }, [range.start, range.end, dates])

  const onChartReady = (inst: any) => {
    inst.group = group
    echarts.connect(group)
  }

  return <ReactECharts ref={chartRef} option={option} style={{ height }} notMerge lazyUpdate onChartReady={onChartReady} />
}
