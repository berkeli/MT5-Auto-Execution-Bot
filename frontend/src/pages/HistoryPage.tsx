import { useState, useEffect, useMemo, useCallback } from 'react'
import { fetchHistory, clearHistory } from '../api'
import { Seg } from '../components/Seg'
import { PerformanceBreakdown } from '../components/PerformanceBreakdown'
import { money } from '../utils/money'
import {
  computeDetailedStats,
  computePerformanceBreakdown,
  formatHoldTime,
  outcomeOf,
} from '../utils/stats'
import { directionFromOrderType } from '../utils/orderType'
import { badgeClassFor, formatSignalType } from '../utils/signalType'
import { ASSET_BASKET_LABELS, getAssetBasket } from '../utils/assetClass'
import { getChannelLabel } from '../utils/channels'
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
  const [channelFilter, setChannelFilter] = useState('all')
  const [basketFilter, setBasketFilter] = useState('all')
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

  const uniqueChannels = useMemo(() => {
    const channels = [
      ...new Set(allGroups.map(g => g.channelId).filter((id): id is string => !!id)),
    ]
    return channels.sort((a, b) => getChannelLabel(a).localeCompare(getChannelLabel(b)))
  }, [allGroups])

  const availableBaskets = useMemo(() => {
    return [...new Set(allGroups.map(g => getAssetBasket(g.symbol)))].sort((a, b) =>
      ASSET_BASKET_LABELS[a].localeCompare(ASSET_BASKET_LABELS[b])
    )
  }, [allGroups])

  const analysisTrades = useMemo(() => {
    return trades.filter(trade => {
      if (instrumentFilter !== 'all' && trade.symbol !== instrumentFilter) return false
      if (channelFilter !== 'all' && trade.channel_id !== channelFilter) return false
      if (basketFilter !== 'all' && getAssetBasket(trade.symbol) !== basketFilter) return false
      if (typeFilter !== 'all' && trade.signal_type !== typeFilter) return false
      return true
    })
  }, [trades, instrumentFilter, channelFilter, basketFilter, typeFilter])

  const filteredGroups = useMemo(() => {
    let rows = allGroups
    if (instrumentFilter !== 'all') rows = rows.filter(g => g.symbol === instrumentFilter)
    if (channelFilter !== 'all') rows = rows.filter(g => g.channelId === channelFilter)
    if (basketFilter !== 'all') rows = rows.filter(g => getAssetBasket(g.symbol) === basketFilter)
    if (statusFilter !== 'all') rows = rows.filter(g => g.status === statusFilter)
    if (typeFilter !== 'all') {
      rows = rows.filter(g => g.signalType === typeFilter)
    }
    return sortGroups(rows, sortBy)
  }, [allGroups, instrumentFilter, channelFilter, basketFilter, statusFilter, typeFilter, sortBy])

  const detailedStats = useMemo(() => computeDetailedStats(analysisTrades), [analysisTrades])
  const tradeCount = analysisTrades.filter(t => t.status === 'closed').length
  const channelBreakdown = useMemo(
    () =>
      computePerformanceBreakdown(analysisTrades, trade => ({
        key: trade.channel_id ?? 'unknown',
        label: getChannelLabel(trade.channel_id),
      })),
    [analysisTrades]
  )
  const basketBreakdown = useMemo(
    () =>
      computePerformanceBreakdown(analysisTrades, trade => {
        const basket = getAssetBasket(trade.symbol)
        return { key: basket, label: ASSET_BASKET_LABELS[basket] }
      }),
    [analysisTrades]
  )
  const hasReportFilters =
    instrumentFilter !== 'all' ||
    channelFilter !== 'all' ||
    basketFilter !== 'all' ||
    typeFilter !== 'all' ||
    statusFilter !== 'closed' ||
    sortBy !== 'newest'

  function resetReportFilters() {
    setInstrumentFilter('all')
    setChannelFilter('all')
    setBasketFilter('all')
    setTypeFilter('all')
    setStatusFilter('closed')
    setSortBy('newest')
  }

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
        <div className="eyebrow">Reporting</div>
        <h2 style={{ margin: '4px 0 0', fontSize: 24, fontWeight: 700, letterSpacing: '-0.01em' }}>
          Performance report
        </h2>
      </div>

      {/* FILTERS */}
      <div className="panel pad">
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div className="field">
            <label htmlFor="history-from">From</label>
            <input
              id="history-from"
              type="date"
              className="inp mono"
              value={fromDate}
              onChange={e => setFromDate(e.target.value)}
              style={{ width: 160, colorScheme: 'light' }}
            />
          </div>
          <div className="field">
            <label htmlFor="history-to">To</label>
            <input
              id="history-to"
              type="date"
              className="inp mono"
              value={toDate}
              onChange={e => setToDate(e.target.value)}
              style={{ width: 160, colorScheme: 'light' }}
            />
          </div>
          <div className="field">
            <label htmlFor="history-instrument">Instrument</label>
            <select
              id="history-instrument"
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
            <label htmlFor="history-basket">Basket</label>
            <select
              id="history-basket"
              className="inp"
              value={basketFilter}
              onChange={e => setBasketFilter(e.target.value)}
            >
              <option value="all">All baskets</option>
              {availableBaskets.map(basket => (
                <option key={basket} value={basket}>
                  {ASSET_BASKET_LABELS[basket]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="history-channel">Channel</label>
            <select
              id="history-channel"
              className="inp"
              value={channelFilter}
              onChange={e => setChannelFilter(e.target.value)}
            >
              <option value="all">All channels</option>
              {uniqueChannels.map(channel => (
                <option key={channel} value={channel}>
                  {getChannelLabel(channel)}
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
            <label htmlFor="history-sort">Sort by</label>
            <select
              id="history-sort"
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
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          {hasReportFilters && (
            <button className="btn sm ghost" onClick={resetReportFilters}>
              Reset filters
            </button>
          )}
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

      {/* STATISTICS */}
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

      {trades.length > 0 && (
        <div className="breakdown-stack">
          <PerformanceBreakdown
            title="By basket"
            subtitle="Compare the markets carrying your result"
            rows={basketBreakdown}
          />
          <PerformanceBreakdown
            title="By channel"
            subtitle="See which signal sources are producing the edge"
            rows={channelBreakdown}
          />
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
                    <span className="signal-origin">
                      {ASSET_BASKET_LABELS[getAssetBasket(g.symbol)]} ·{' '}
                      {getChannelLabel(g.channelId)}
                    </span>
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
