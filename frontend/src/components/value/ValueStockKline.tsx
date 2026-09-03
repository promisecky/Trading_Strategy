import { useEffect, useMemo, useRef } from 'react'
import ReactECharts from 'echarts-for-react'
import * as echarts from 'echarts'
import type { ValueKlineResponse } from '../../api/types'

const RULE_LABEL: Record<number, string> = { 1: '①动态止损', 2: '②移动止盈', 3: '③bias≥1.08' }

/** 个股 K 线(对标 ETF 走势对比): 蜡烛图 + MA120 + 买/卖标记;
 *  high/low 缺失(升级抓取中)时自动降级为收盘价折线。
 *  group 非空时与同组图表(指标曲线)联动缩放。 */
export default function ValueStockKline({ data, height = 340, group, range }: {
  data: ValueKlineResponse
  height?: number
  group?: string
  range?: { start?: string; end?: string }
}) {
  const chartRef = useRef<any>(null)
  const hasHl = useMemo(() => data.kline.some(k => k.high != null && k.low != null), [data])
  const dates = useMemo(() => data.kline.map(k => k.date), [data])

  // 时间范围 → 百分比缩放
  useEffect(() => {
    const inst = chartRef.current?.getEchartsInstance?.()
    if (!inst || !dates.length || (!range?.start && !range?.end)) return
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
  }, [range?.start, range?.end, dates])

  const onChartReady = (inst: any) => {
    if (group) {
      inst.group = group
      echarts.connect(group)
    }
  }

  const { option } = useMemo(() => {
    const byDate = new Map(data.kline.map(k => [k.date, k]))
    // A股配色: 涨红跌绿
    const candles = data.kline.map(k => [k.open, k.close, k.low ?? k.close, k.high ?? k.close])

    const tradeMarks = data.trades
      .filter(t => byDate.has(t.exec_date))
      .map(t => {
        const k = byDate.get(t.exec_date)!
        const isBuy = t.action === 'BUY'
        const anchor = hasHl ? (isBuy ? (k.low ?? k.close) * 0.995 : (k.high ?? k.close) * 1.005) : k.close
        const label = isBuy
          ? t.kind === 'side' ? '买·侧' : '买'
          : `卖·${RULE_LABEL[t.rule ?? 0] ?? ''}`
        return {
          coord: [t.exec_date, anchor],
          value: isBuy ? 'B' : 'S',
          symbol: isBuy ? 'triangle' : 'pin',
          symbolSize: isBuy ? 16 : 18,
          symbolRotate: isBuy ? 0 : 180,
          itemStyle: { color: isBuy ? '#15803d' : '#ef4444' },
          label: { show: true, formatter: label, fontSize: 9, color: '#fff', offset: [0, isBuy ? 5 : -5] as [number, number] },
          _trade: t,
        }
      })

    const tooltipFmt = (params: any) => {
      const list = Array.isArray(params) ? params : [params]
      const p = list.find((x: any) => x.seriesName === 'K线') ?? list[0]
      if (!p) return ''
      const d = dates[p.dataIndex]
      const k = byDate.get(d)
      if (!k) return ''
      let html = `<b>${d}</b><br/>开 ${k.open} · 收 <b>${k.close}</b>`
      if (k.high != null && k.low != null) html += `<br/>高 ${k.high} · 低 ${k.low}`
      const ma = list.find((x: any) => x.seriesName === 'MA120')
      if (ma?.value != null) html += `<br/><span style="color:#f59e0b">MA120 ${Number(ma.value).toFixed(2)}</span>`
      for (const t of data.trades.filter(t => t.exec_date === d)) {
        const color = t.action === 'BUY' ? '#22c55e' : '#ef4444'
        const label = t.action === 'BUY' ? (t.kind === 'side' ? '买入·侧门' : '买入·主信号') : `卖出·${RULE_LABEL[t.rule ?? 0] ?? ''}`
        html += `<br/><span style="color:${color};font-weight:bold">◆ ${label} @${t.price} — ${t.reason}</span>`
      }
      return html
    }

    return {
      option: {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross', lineStyle: { color: '#4b5563' } },
          backgroundColor: 'rgba(17,24,39,0.95)',
          borderColor: '#374151',
          textStyle: { color: '#e5e7eb', fontSize: 11 },
          formatter: tooltipFmt,
        },
        grid: { left: 56, right: 16, top: 24, bottom: 52 },
        xAxis: {
          type: 'category',
          data: dates,
          axisLine: { lineStyle: { color: '#374151' } },
          axisLabel: { color: '#9ca3af', fontSize: 10 },
        },
        yAxis: {
          type: 'value',
          scale: true,
          splitLine: { lineStyle: { color: '#1f2937' } },
          axisLabel: { color: '#9ca3af', fontSize: 10 },
        },
        dataZoom: [
          { type: 'inside', start: 70, end: 100 },
          { type: 'slider', height: 16, bottom: 8, borderColor: '#374151', textStyle: { color: '#9ca3af', fontSize: 10 } },
        ],
        series: hasHl
          ? [
              {
                name: 'K线',
                type: 'candlestick',
                data: candles,
                itemStyle: { color: '#ef4444', color0: '#22c55e', borderColor: '#ef4444', borderColor0: '#22c55e' },
                markPoint: { clip: false, data: tradeMarks },
              },
              {
                name: 'MA120',
                type: 'line',
                data: data.ma120,
                showSymbol: false,
                lineStyle: { color: '#f59e0b', width: 1 },
              },
            ]
          : [
              {
                name: 'K线',
                type: 'line',
                data: data.kline.map(k => k.close),
                showSymbol: false,
                lineStyle: { color: '#38bdf8', width: 1.2 },
                markPoint: { clip: false, data: tradeMarks },
              },
              {
                name: 'MA120',
                type: 'line',
                data: data.ma120,
                showSymbol: false,
                lineStyle: { color: '#f59e0b', width: 1 },
              },
            ],
      },
    }
  }, [data, hasHl, dates])

  return <ReactECharts ref={chartRef} option={option} style={{ height }} notMerge lazyUpdate onChartReady={onChartReady} />
}
