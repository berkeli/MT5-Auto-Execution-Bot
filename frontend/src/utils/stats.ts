import type { TradeData } from '../types'

export type Outcome = 'win' | 'loss' | 'breakeven'

/** A signal the TM flattened at breakeven scores neither win nor loss — its residual
 * P&L is spread/commission, not a directional result. An exactly-flat close is
 * treated the same so it can't sit in the win-rate denominator as a phantom loss. */
export function outcomeOf(t: TradeData): Outcome {
  if (t.breakeven || t.total_pnl === 0) return 'breakeven'
  return t.total_pnl > 0 ? 'win' : 'loss'
}

export interface WinLossCounts {
  wins: number
  losses: number
  breakevens: number
  winRate: number
}

export function countOutcomes(trades: TradeData[]): WinLossCounts {
  let wins = 0,
    losses = 0,
    breakevens = 0
  for (const t of trades) {
    if (t.status !== 'closed') continue
    const o = outcomeOf(t)
    if (o === 'win') wins++
    else if (o === 'loss') losses++
    else breakevens++
  }
  const decided = wins + losses
  return { wins, losses, breakevens, winRate: decided > 0 ? (wins / decided) * 100 : 0 }
}

export interface DetailedStats extends WinLossCounts {
  netPnl: number
  profitFactor: number
  expectancy: number
  avgWin: number
  avgLoss: number
  bestTrade: { pnl: number; symbol: string }
  worstTrade: { pnl: number; symbol: string }
  bestStreak: number
  worstStreak: number
  avgHoldMinutes: number
  scalpShare: number
  totalTrades: number
}

function closeTime(t: TradeData): string {
  return t.closed_at || t.filled_at || t.placed_at
}

export interface PerformanceBreakdown {
  key: string
  label: string
  trades: number
  wins: number
  losses: number
  breakevens: number
  winRate: number
  netPnl: number
  avgPnl: number
  profitFactor: number
  tradeShare: number
}

export function computePerformanceBreakdown(
  trades: TradeData[],
  groupFor: (trade: TradeData) => { key: string; label: string }
): PerformanceBreakdown[] {
  const closed = trades.filter(t => t.status === 'closed')
  const groups = new Map<string, { label: string; trades: TradeData[] }>()

  for (const trade of closed) {
    const { key, label } = groupFor(trade)
    const group = groups.get(key) ?? { label, trades: [] }
    group.trades.push(trade)
    groups.set(key, group)
  }

  return [...groups.entries()]
    .map(([key, group]) => {
      const counts = countOutcomes(group.trades)
      const wins = group.trades.filter(t => outcomeOf(t) === 'win')
      const losses = group.trades.filter(t => outcomeOf(t) === 'loss')
      const grossProfit = wins.reduce((sum, trade) => sum + trade.total_pnl, 0)
      const grossLoss = Math.abs(losses.reduce((sum, trade) => sum + trade.total_pnl, 0))
      const netPnl = group.trades.reduce((sum, trade) => sum + trade.total_pnl, 0)

      return {
        key,
        label: group.label,
        trades: group.trades.length,
        ...counts,
        netPnl,
        avgPnl: netPnl / group.trades.length,
        profitFactor: grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0,
        tradeShare: (group.trades.length / closed.length) * 100,
      }
    })
    .sort((a, b) => b.netPnl - a.netPnl || b.trades - a.trades || a.label.localeCompare(b.label))
}

export function computeDetailedStats(trades: TradeData[]): DetailedStats {
  const closed = trades
    .filter(t => t.status === 'closed')
    .sort((a, b) => closeTime(a).localeCompare(closeTime(b)))
  const counts = countOutcomes(closed)
  const wins = closed.filter(t => outcomeOf(t) === 'win')
  const losses = closed.filter(t => outcomeOf(t) === 'loss')

  const grossWin = wins.reduce((s, t) => s + t.total_pnl, 0)
  const grossLoss = Math.abs(losses.reduce((s, t) => s + t.total_pnl, 0))
  const netPnl = closed.reduce((s, t) => s + t.total_pnl, 0)

  const best = closed.reduce(
    (b, t) => (t.total_pnl > b.pnl ? { pnl: t.total_pnl, symbol: t.symbol } : b),
    { pnl: -Infinity, symbol: '' }
  )
  const worst = closed.reduce(
    (w, t) => (t.total_pnl < w.pnl ? { pnl: t.total_pnl, symbol: t.symbol } : w),
    { pnl: Infinity, symbol: '' }
  )

  // Longest run of each outcome, chronologically. Breakevens are transparent —
  // they neither extend nor break a run.
  let bestStreak = 0,
    worstStreak = 0,
    curWin = 0,
    curLoss = 0
  for (const t of closed) {
    const o = outcomeOf(t)
    if (o === 'breakeven') continue
    if (o === 'win') {
      curWin++
      curLoss = 0
    } else {
      curLoss++
      curWin = 0
    }
    bestStreak = Math.max(bestStreak, curWin)
    worstStreak = Math.max(worstStreak, curLoss)
  }

  let totalHold = 0,
    holdCount = 0
  for (const t of closed) {
    const closeTs = closeTime(t)
    if (t.filled_at && closeTs) {
      const ms = new Date(closeTs).getTime() - new Date(t.filled_at).getTime()
      if (ms > 0) {
        totalHold += ms
        holdCount++
      }
    }
  }

  const scalps = closed.filter(t => t.signal_type === 'scalp').length

  return {
    ...counts,
    netPnl,
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0,
    expectancy: closed.length > 0 ? netPnl / closed.length : 0,
    avgWin: wins.length > 0 ? grossWin / wins.length : 0,
    avgLoss: losses.length > 0 ? -(grossLoss / losses.length) : 0,
    bestTrade: best.pnl === -Infinity ? { pnl: 0, symbol: '—' } : best,
    worstTrade: worst.pnl === Infinity ? { pnl: 0, symbol: '—' } : worst,
    bestStreak,
    worstStreak,
    avgHoldMinutes: holdCount > 0 ? totalHold / holdCount / 60000 : 0,
    scalpShare: closed.length > 0 ? (scalps / closed.length) * 100 : 0,
    totalTrades: closed.length,
  }
}

export interface DailyBar {
  date: string
  label: string
  value: number
}

export function computeDailyBars(trades: TradeData[]): DailyBar[] {
  const byDay = new Map<string, number>()
  for (const t of trades) {
    if (t.status !== 'closed') continue
    const ts = t.closed_at || t.filled_at || t.placed_at
    if (!ts) continue
    const day = ts.slice(0, 10)
    byDay.set(day, (byDay.get(day) ?? 0) + t.total_pnl)
  }
  const sorted = [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b))
  return sorted.slice(-14).map(([date, value]) => {
    const d = new Date(date + 'T00:00:00')
    const label = String(d.getDate())
    const month = d.toLocaleDateString('en', { month: 'short' })
    return { date: `${month} ${label}`, label, value: Math.round(value * 100) / 100 }
  })
}

export interface CurvePoint {
  label: string
  value: number
}

export function computeCumulativePnl(trades: TradeData[]): CurvePoint[] {
  const closed = trades
    .filter(t => t.status === 'closed' && (t.closed_at || t.filled_at || t.placed_at))
    .sort((a, b) => {
      const ta = a.closed_at || a.filled_at || a.placed_at
      const tb = b.closed_at || b.filled_at || b.placed_at
      return ta.localeCompare(tb)
    })

  if (closed.length === 0) return [{ label: 'Start', value: 0 }]

  const points: CurvePoint[] = [{ label: 'Start', value: 0 }]
  let cum = 0
  for (const t of closed) {
    cum += t.total_pnl
    const ts = t.closed_at || t.filled_at || t.placed_at
    const d = new Date(ts)
    const label = d.toLocaleDateString('en', { month: 'short', day: 'numeric' })
    points.push({ label, value: Math.round(cum * 100) / 100 })
  }
  return points
}

export function formatHoldTime(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return `${h}h ${m}m`
}

export type Period = 'daily' | 'weekly' | 'all'

export function filterTradesByPeriod(trades: TradeData[], period: Period): TradeData[] {
  if (period === 'all') return trades
  const cutoff = Date.now() - (period === 'daily' ? 86400000 : 7 * 86400000)
  return trades.filter(t => {
    const ts = t.closed_at || t.filled_at || t.placed_at
    return ts && new Date(ts).getTime() >= cutoff
  })
}

export function groupBySignalId<T extends { signal_id: number }>(items: T[]): Map<number, T[]> {
  const map = new Map<number, T[]>()
  for (const item of items) {
    const group = map.get(item.signal_id) ?? []
    group.push(item)
    map.set(item.signal_id, group)
  }
  return map
}
