import { useState, useEffect, useMemo, useCallback } from 'react'
import { fetchHistory, clearHistory } from '../api'
import { Seg } from '../components/Seg'
import { money } from '../utils/money'
import { computeDetailedStats, formatHoldTime, outcomeOf } from '../utils/stats'
import { directionFromOrderType } from '../utils/orderType'
import { badgeClassFor, formatSignalType } from '../utils/signalType'
import type { HistoryData, SignalType, TradeData } from '../types'

function todayStr(): string {
  return new Date().toISOString().slice(0, 10)
}

function monthAgoStr(): string {
  const d = new Date()
  d.setMonth(d.getMonth() - 1)
  return d.toISOString().slice(0, 10)
}

function formatTime(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return (
    d.toLocaleDateString('en', { month: 'short', day: 'numeric' }) +
    ' · ' +
    d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  )
}

interface SignalGroup {
  signalId: number
  symbol: string
  direction: 'long' | 'short'
  totalLots: number
  totalPnl: number
  tradeCount: number
  closedAt: string
  status: string
  breakeven: boolean
  channelId: string | null
  signalType: SignalType
}

function tradeToGroup(t: TradeData): SignalGroup {
  return {
    signalId: t.signal_id,
    symbol: t.symbol,
    direction: directionFromOrderType(t.direction),
    totalLots: t.total_lots,
    totalPnl: t.total_pnl,
    tradeCount: t.fills_count + t.cancelled_count,
    closedAt: t.closed_at || t.filled_at || t.placed_at,
    status: t.status,
    breakeven: t.status === 'closed' && outcomeOf(t) === 'breakeven',
    channelId: t.channel_id,
    signalType: (t.signal_type ?? 'standard') as SignalType,
  }
}

type SortKey = 'newest' | 'oldest' | 'pnl_high' | 'pnl_low' | 'symbol'

function sortGroups(groups: SignalGroup[], by: SortKey): SignalGroup[] {
  const s = [...groups]
  switch (by) {
    case 'newest':
      return s.sort((a, b) => b.closedAt.localeCompare(a.closedAt))
    case 'oldest':
      return s.sort((a, b) => a.closedAt.localeCompare(b.closedAt))
    case 'pnl_high':
      return s.sort((a, b) => b.totalPnl - a.totalPnl)
    case 'pnl_low':
      return s.sort((a, b) => a.totalPnl - b.totalPnl)
    case 'symbol':
      return s.sort((a, b) => a.symbol.localeCompare(b.symbol))
  }
}

export function HistoryPage() {
  const [fromDate, setFromDate] = useState(monthAgoStr)
  const [toDate, setToDate] = useState(todayStr)
  const [data, setData] = useState<HistoryData | null>(null)
  const [instrumentFilter, setInstrumentFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState('closed')
  const [sortBy, setSortBy] = useState<SortKey>('newest')
  const [confirmClear, setConfirmClear] = useState(false)
  const [clearing, setClearing] = useState(false)

  const load = useCallback(() => {
    const from = `${fromDate}T00:00:00`
    const to = `${toDate}T23:59:59`
    fetchHistory(from, to)
      .then(setData)
      .catch(() => {})
  }, [fromDate, toDate])

  useEffect(() => {
    load()
  }, [load])

  async function handleClear() {
    setClearing(true)
    try {
      await clearHistory()
      setConfirmClear(false)
      load()
    } catch {
      /* keep the dialog open so the user can retry */
    } finally {
      setClearing(false)
    }
  }

  const trades: TradeData[] = data?.trades ?? []

  const allGroups = useMemo(() => trades.map(tradeToGroup), [trades])

  const uniqueSymbols = useMemo(() => {
    const syms = [...new Set(allGroups.map(g => g.symbol).filter(Boolean))]
    return syms.sort()
  }, [allGroups])

  const filteredGroups = useMemo(() => {
    let rows = allGroups
    if (instrumentFilter !== 'all') rows = rows.filter(g => g.symbol === instrumentFilter)
    if (statusFilter !== 'all') rows = rows.filter(g => g.status === statusFilter)
    if (typeFilter !== 'all') {
      rows = rows.filter(g => g.signalType === typeFilter)
    }
    return sortGroups(rows, sortBy)
  }, [allGroups, instrumentFilter, statusFilter, typeFilter, sortBy])

  const detailedStats = useMemo(() => computeDetailedStats(trades), [trades])
  const tradeCount = trades.filter(t => t.status === 'closed').length

  const stat = (label: string, value: string, cls?: string, note?: string, small?: boolean) => (
    <div className="statcell">
      <div className="l">{label}</div>
      <div className={`v ${small ? 's ' : ''}${cls || ''}`}>{value}</div>
      {note && <div className="n">{note}</div>}
    </div>
  )

  return (
    <div className="page">
      <div>
        <div className="eyebrow">Analytics</div>
        <h2 style={{ margin: '4px 0 0', fontSize: 24, fontWeight: 700, letterSpacing: '-0.01em' }}>
          Trade history
        </h2>
      </div>

      {/* FILTERS */}
      <div className="panel pad">
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div className="field">
            <label>From</label>
            <input
              type="date"
              className="inp mono"
              value={fromDate}
              onChange={e => setFromDate(e.target.value)}
              style={{ width: 160, colorScheme: 'light' }}
            />
          </div>
          <div className="field">
            <label>To</label>
            <input
              type="date"
              className="inp mono"
              value={toDate}
              onChange={e => setToDate(e.target.value)}
              style={{ width: 160, colorScheme: 'light' }}
            />
          </div>
          <div className="field">
            <label>Instrument</label>
            <select
              className="inp"
              value={instrumentFilter}
              onChange={e => setInstrumentFilter(e.target.value)}
            >
              <option value="all">All</option>
              {uniqueSymbols.map(s => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Status</label>
            <Seg
              value={statusFilter}
              options={[
                { value: 'all', label: 'All' },
                { value: 'closed', label: 'Closed' },
                { value: 'cancelled', label: 'Cancelled' },
              ]}
              onChange={setStatusFilter}
            />
          </div>
          <div className="field">
            <label>Type</label>
            <Seg
              value={typeFilter}
              options={[
                { value: 'all', label: 'All' },
                { value: 'standard', label: 'Standard' },
                { value: 'scalp', label: 'Scalp' },
                { value: 'swing', label: 'Swing' },
                { value: 'toll', label: 'Toll' },
                { value: 'pa', label: 'PA' },
                { value: '1-1', label: '1-1' },
                { value: 'risky', label: 'Risky' },
              ]}
              onChange={setTypeFilter}
            />
          </div>
          <div className="field">
            <label>Sort by</label>
            <select
              className="inp"
              value={sortBy}
              onChange={e => setSortBy(e.target.value as SortKey)}
            >
              <option value="newest">Newest</option>
              <option value="oldest">Oldest</option>
              <option value="pnl_high">P&amp;L High → Low</option>
              <option value="pnl_low">P&amp;L Low → High</option>
              <option value="symbol">Symbol A → Z</option>
            </select>
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
          <button className="btn sm danger-solid" onClick={() => setConfirmClear(true)}>
            Clear history
          </button>
        </div>
      </div>

      {confirmClear && (
        <div className="modal-overlay" onClick={() => !clearing && setConfirmClear(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-title">Clear history?</div>
            <p className="modal-notes">
              This resets all statistics and the visuals on the dashboard — equity curve, win/loss,
              P&amp;L and every trade record are wiped, and the account is treated as new (starting
              balance becomes the current balance, P&amp;L back to 0).
            </p>
            <div className="modal-warn">
              Your current open positions and pending orders are not touched — only past trade
              history is cleared. This can’t be undone.
            </div>
            <div className="modal-actions">
              <button
                className="btn ghost"
                onClick={() => setConfirmClear(false)}
                disabled={clearing}
              >
                Cancel
              </button>
              <button className="btn danger-solid" onClick={handleClear} disabled={clearing}>
                {clearing ? 'Clearing…' : 'Clear history'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* STATISTICS — trade-level, unaffected by filters */}
      {trades.length > 0 && (
        <div className="panel" style={{ overflow: 'hidden' }}>
          <div className="panel-head" style={{ padding: '20px 22px 0', marginBottom: 0 }}>
            <h3>
              Performance{' '}
              <span className="sub" style={{ fontWeight: 400 }}>
                — {tradeCount} trades
              </span>
            </h3>
          </div>
          <div className="statgrid" style={{ marginTop: 18 }}>
            {stat(
              'Net P&L',
              money(detailedStats.netPnl),
              detailedStats.netPnl >= 0 ? 'pos' : 'neg'
            )}
            {stat(
              'Win rate',
              `${detailedStats.winRate.toFixed(0)}%`,
              '',
              `${detailedStats.wins} W · ${detailedStats.losses} L` +
                (detailedStats.breakevens > 0 ? ` · ${detailedStats.breakevens} BE` : '')
            )}
            {stat(
              'Profit factor',
              detailedStats.profitFactor === Infinity ? '∞' : detailedStats.profitFactor.toFixed(2)
            )}
            {stat(
              'Expectancy',
              money(detailedStats.expectancy),
              detailedStats.expectancy >= 0 ? 'pos' : 'neg',
              'avg per trade'
            )}
            {stat(
              'Average win',
              money(detailedStats.avgWin),
              'pos',
              `across ${detailedStats.wins} wins`,
              true
            )}
            {stat(
              'Average loss',
              money(detailedStats.avgLoss),
              'neg',
              `across ${detailedStats.losses} losses`,
              true
            )}
            {stat(
              'Best trade',
              money(detailedStats.bestTrade.pnl),
              detailedStats.bestTrade.pnl >= 0 ? 'pos' : 'neg',
              detailedStats.bestTrade.symbol,
              true
            )}
            {stat(
              'Worst trade',
              money(detailedStats.worstTrade.pnl),
              detailedStats.worstTrade.pnl > 0 ? 'pos' : 'neg',
              detailedStats.worstTrade.symbol,
              true
            )}
            {stat(
              'Longest streak',
              `${detailedStats.bestStreak}W · ${detailedStats.worstStreak}L`,
              '',
              'consecutive, best and worst',
              true
            )}
            {stat(
              'Breakeven',
              String(detailedStats.breakevens),
              '',
              'excluded from win rate',
              true
            )}
            {stat(
              'Avg hold',
              formatHoldTime(detailedStats.avgHoldMinutes),
              '',
              'open → close',
              true
            )}
            {stat('Scalp share', `${detailedStats.scalpShare.toFixed(0)}%`, '', undefined, true)}
          </div>
        </div>
      )}

      {/* SIGNALS TABLE */}
      <div className="panel pad">
        <div className="panel-head">
          <h3>Signals</h3>
          <span className="sub">{filteredGroups.length} groups</span>
        </div>
        {filteredGroups.length === 0 ? (
          <p className="faint" style={{ padding: '12px 0' }}>
            No trades match filters
          </p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th className="num">ID</th>
                <th>Closed</th>
                <th>Symbol</th>
                <th>Side</th>
                <th className="num">Limits</th>
                <th className="num">Total Lots</th>
                <th>Type</th>
                <th>Status</th>
                <th className="num">Total P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {filteredGroups.map(g => (
                <tr key={g.signalId}>
                  <td className="num mono dim">{g.signalId}</td>
                  <td className="t-sub mono">{formatTime(g.closedAt)}</td>
                  <td>
                    <span className="sym">{g.symbol || '—'}</span>
                  </td>
                  <td>
                    <span className={'tag ' + g.direction}>{g.direction}</span>
                  </td>
                  <td className="num mono dim">{g.tradeCount}</td>
                  <td className="num mono">{g.totalLots.toFixed(2)}</td>
                  <td>
                    {g.signalType === 'standard' ? (
                      <span className="t-sub">Standard</span>
                    ) : (
                      <span className={'tag ' + badgeClassFor(g.signalType)}>
                        {formatSignalType(g.signalType)}
                      </span>
                    )}
                  </td>
                  <td>
                    {g.breakeven ? (
                      <span className="tag ghost">breakeven</span>
                    ) : g.status === 'closed' ? (
                      <span className="tag trail">closed</span>
                    ) : (
                      <span className="tag ghost">{g.status}</span>
                    )}
                  </td>
                  <td
                    className={`num mono ${
                      g.breakeven || g.totalPnl === 0 ? 'faint' : g.totalPnl > 0 ? 'pos' : 'neg'
                    }`}
                    style={{ fontWeight: 600 }}
                  >
                    {g.totalPnl === 0 ? '—' : money(g.totalPnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
