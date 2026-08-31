import ReactECharts from 'echarts-for-react'
import { useMemo, useState } from 'react'
import type { ValueHistoryPoint, ValueTrade } from '../../api/types'

const RULE_LABEL: Record<number, string> = { 1: '①止损-12%', 2: '②止盈+10%', 3: '③bias≥1.08' }
const BUY_COLOR = '#4ade80' // 买=绿(与页面信号表一致)
const SELL_COLOR = '#f87171'

interface PopupState {
  left: number
  top: number
  date: string
  items: ValueTrade[]
}

export default function ValueEquityChart({
  history,
  trades = [],
}: {
  history: ValueHistoryPoint[]
  trades?: ValueTrade[]
}) {
  const [popup, setPopup] = useState<PopupState | null>(null)

  const dateIdx = useMemo(() => {
    const m = new Map<string, number>()
    history.forEach((h, i) => m.set(h.date, i))
    return m
  }, [history])

  const tradesByDate = useMemo(() => {
    const m = new Map<string, ValueTrade[]>()
    for (const t of trades) {
      if (!dateIdx.has(t.exec_date)) continue
      const arr = m.get(t.exec_date) ?? []
      arr.push(t)
      m.set(t.exec_date, arr)
    }
    return m
  }, [trades, dateIdx])

  const marks = useMemo(
    () =>
      [...tradesByDate.entries()].flatMap(([date, items]) => {
        const idx = dateIdx.get(date)!
        const y = Number(history[idx].equity.toFixed(4))
        return items.map(t => ({
          value: [idx, y],
          symbol: 'triangle',
          symbolRotate: t.action === 'BUY' ? 0 : 180,
          symbolSize: 11,
          itemStyle: { color: t.action === 'BUY' ? BUY_COLOR : SELL_COLOR },
        }))
      }),
    [tradesByDate, dateIdx, history]
  )

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(17,24,39,0.95)',
      borderColor: '#374151',
      textStyle: { color: '#e5e7eb', fontSize: 11 },
      formatter: (params: any) => {
        const p = params?.[0]
        if (!p) return ''
        const h = history[p.dataIndex]
        let html = `${h.date}<br/>净值 <b>${h.equity.toFixed(4)}</b><br/>仓位 ${h.position_pct.toFixed(1)}%`
        const items = tradesByDate.get(h.date)
        if (items?.length) {
          html += '<br/><span style="color:#6b7280">────────</span>'
          for (const t of items) {
            const color = t.action === 'BUY' ? BUY_COLOR : SELL_COLOR
            const label =
              t.action === 'BUY' ? (t.kind === 'side' ? '买·侧门' : '买·主') : `卖·${RULE_LABEL[t.rule ?? 0] ?? ''}`
            html += `<br/><span style="color:${color}">${label}</span> ${t.code} @${t.price}`
          }
        }
        return html
      },
    },
    grid: { left: 48, right: 16, top: 24, bottom: 48 },
    xAxis: {
      type: 'category',
      data: history.map(h => h.date),
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
      { type: 'inside' },
      { type: 'slider', height: 16, bottom: 8, borderColor: '#374151', textStyle: { color: '#9ca3af', fontSize: 10 } },
    ],
    series: [
      {
        name: '净值',
        type: 'line',
        showSymbol: false,
        data: history.map(h => Number(h.equity.toFixed(4))),
        lineStyle: { color: '#38bdf8', width: 1.5 },
        areaStyle: { color: 'rgba(56,189,248,0.08)' },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: '#6b7280', type: 'dashed' },
          data: [{ yAxis: 1 }],
          label: { color: '#9ca3af', fontSize: 10 },
        },
      },
      {
        name: '买卖点',
        type: 'scatter',
        data: marks,
        z: 5,
        emphasis: { scale: 1.6 },
      },
    ],
  }

  const onEvents = {
    click: (params: any) => {
      if (params.seriesName !== '买卖点') return
      const idx = Array.isArray(params.value) ? params.value[0] : params.dataIndex
      const date = history[idx]?.date
      const items = date ? tradesByDate.get(date) : undefined
      if (!date || !items?.length) return
      const ev = params.event?.event
      const x = ev?.offsetX ?? 120
      const y = ev?.offsetY ?? 120
      setPopup({
        left: Math.max(8, Math.min(x - 110, 640)),
        top: Math.max(8, y - 40 - items.length * 22 - 56),
        date,
        items,
      })
    },
  }

  return (
    <div className="relative">
      <ReactECharts option={option} style={{ height: 280 }} notMerge lazyUpdate onEvents={onEvents} />
      {popup && (
        <div
          className="absolute bg-gray-800 border border-gray-600 rounded-lg p-3 shadow-lg z-10 w-[230px]"
          style={{ left: popup.left, top: popup.top }}
        >
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-bold text-white">{popup.date}</span>
            <button onClick={() => setPopup(null)} className="text-gray-400 hover:text-white text-lg leading-none px-1">
              &times;
            </button>
          </div>
          {popup.items.map((t, i) => (
            <div key={i} className="text-xs mb-1.5 last:mb-0">
              <span className="font-bold" style={{ color: t.action === 'BUY' ? BUY_COLOR : SELL_COLOR }}>
                {t.action === 'BUY' ? (t.kind === 'side' ? '买·侧门' : '买·主') : `卖·${RULE_LABEL[t.rule ?? 0] ?? ''}`}
              </span>
              <span className="text-gray-300 ml-1 font-mono">{t.code}</span>
              <span className="text-gray-500 ml-1 font-mono">
                {t.price} × {t.shares}
              </span>
              {t.pnl_pct != null && (
                <span className={`ml-1 font-mono ${t.pnl_pct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                  {t.pnl_pct > 0 ? '+' : ''}
                  {t.pnl_pct}%
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
