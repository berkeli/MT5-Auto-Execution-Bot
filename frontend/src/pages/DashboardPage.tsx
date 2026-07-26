import { useState, useMemo } from 'react'
import { Icon } from '../components/Icon'
import { Seg } from '../components/Seg'
import { SignalMenu } from '../components/SignalMenu'
import { ProxMeter } from '../components/ProxMeter'
import { setSignalAction } from '../api'
import type { SignalAction } from '../types'
import { EquityCurve } from '../charts/EquityCurve'
import { Donut } from '../charts/Donut'
import { Bars } from '../charts/Bars'
import { useSort } from '../hooks/useSort'
import { money } from '../utils/money'
import {
  computeCumulativePnl,
  computeDailyBars,
  countOutcomes,
  filterTradesByPeriod,
  outcomeOf,
} from '../utils/stats'
import type { Period } from '../utils/stats'
import { getChannelName } from '../utils/channels'
import { directionFromOrderType } from '../utils/orderType'
import { formatSignalType } from '../utils/signalType'
import { WarningBanners } from '../components/WarningBanners'
import type { Config, DashboardData, HistoryData, Page, StatusData, TradeData } from '../types'

interface Props {
  dashboard: DashboardData | null
  history: HistoryData | null
  config: Config | null
  status: StatusData | null
  onNavigate: (page: Page) => void
}

export function DashboardPage({ dashboard, history, config, status, onNavigate }: Props) {
  const licenseMissing = config !== null && !config.license_key
  const [pnlP, setPnlP] = useState<Period>('all')
  const [wlP, setWlP] = useState<Period>('all')
  const [showAll, setShowAll] = useState(false)

  const positions = dashboard?.positions ?? []
  const nearbySignals = dashboard?.nearby_signals ?? []
  const totalPnl = positions.reduce((s, p) => s + p.profit, 0)

  const trades: TradeData[] = history?.trades ?? []

  // Period-filtered slices
  const filteredForCurve = useMemo(() => filterTradesByPeriod(trades, pnlP), [trades, pnlP])
  const filteredForWL = useMemo(() => filterTradesByPeriod(trades, wlP), [trades, wlP])

  const curve = useMemo(() => computeCumulativePnl(filteredForCurve), [filteredForCurve])
  const dailyBars = useMemo(() => computeDailyBars(filteredForCurve), [filteredForCurve])

  const curveData = curve.map(p => p.value)
  const curveLabels = curve.map(p => p.label)
  const pnlValue = curve.length > 0 ? curve[curve.length - 1].value : 0
  const pnlLabel =
    pnlP === 'daily'
      ? "Today's P&L"
      : pnlP === 'weekly'
        ? 'P&L · this week'
        : 'Cumulative P&L · all time'

  const wl = useMemo(() => countOutcomes(filteredForWL), [filteredForWL])

  // Positions table
  const posRows = useMemo(
    () =>
      positions.map(p => ({
        sym: p.symbol,
        side: p.direction as 'long' | 'short',
        lot: p.volume,
        entry: p.price_open,
        cur: p.current_price,
        pnl: p.profit,
        trailing: p.is_trailing,
        ticket: p.ticket,
      })),
    [positions]
  )

  const pos = useSort(posRows, 'pnl')

  // Closest Signals: every active Supabase signal (placed or watching), sorted by
  // proximity bar fill — closeness relative to each instrument's own threshold.
  const signalGroups = useMemo(() => {
    return nearbySignals.map(s => ({
      signal_id: s.signal_id,
      sym: s.symbol,
      side: s.direction as 'long' | 'short',
      closestPrice: s.closest_price_display,
      pct: s.proximity_pct,
      dist: s.distance_display,
      channelName: getChannelName(s.channel_id),
      signalType: formatSignalType(s.signal_type),
      placed: s.placed,
      action: s.action,
    }))
  }, [nearbySignals])

  const visibleGroups = showAll ? signalGroups : signalGroups.slice(0, 3)

  async function handleSignalAction(signalId: number, action: SignalAction) {
    try {
      await setSignalAction(signalId, action)
    } catch {
      /* dashboard poll will re-sync the badge */
    }
  }

  // Recent Trades: backend already returns one row per signal_id
  const recentTradeGroups = useMemo(() => {
    return trades
      .filter(t => t.status === 'closed')
      .map(t => ({
        signal_id: t.signal_id,
        symbol: t.symbol,
        side: directionFromOrderType(t.direction),
        limitCount: t.fills_count,
        totalPnl: t.total_pnl,
        outcome: outcomeOf(t),
        closedAt: t.closed_at || t.filled_at || t.placed_at,
      }))
      .sort((a, b) => b.closedAt.localeCompare(a.closedAt))
      .slice(0, 5)
  }, [trades])

  const periods = [
    { value: 'daily', label: 'Day' },
    { value: 'weekly', label: 'Week' },
    { value: 'all', label: 'All' },
  ]

  return (
    <div className="page">
      {licenseMissing && (
        <div className="license-banner">
          <Icon name="bell" size={18} />
          <div className="license-banner-body">
            <div className="license-banner-title">License key not set</div>
            <div className="license-banner-text">
              Run <span className="mono">!activate</span> in the bot-commands channel of the
              Trademaster Discord to get your key, then paste it into Settings.
            </div>
          </div>
          <button className="btn sm" onClick={() => onNavigate('settings')}>
            Go to Settings
          </button>
        </div>
      )}
      <WarningBanners status={status} config={config} />
      {/* HERO */}
      <div className="row">
        <div className="panel pad" style={{ flex: 2.1, minWidth: 0 }}>
          <div className="panel-head">
            <div>
              <div className="eyebrow">{pnlLabel}</div>
              <div className="metric" style={{ marginTop: 10 }}>
                <span className={`big mono ${pnlValue >= 0 ? 'pos' : 'neg'}`}>
                  {money(pnlValue)}
                </span>
              </div>
            </div>
            <Seg value={pnlP} options={periods} onChange={v => setPnlP(v as Period)} />
          </div>
          <EquityCurve data={curveData} labels={curveLabels} height={210} />
          {curveLabels.length > 2 && (
            <div className="axisrow">
              {[
                curveLabels[0],
                curveLabels[Math.floor(curveLabels.length / 4)],
                curveLabels[Math.floor(curveLabels.length / 2)],
                curveLabels[Math.floor((curveLabels.length * 3) / 4)],
                curveLabels[curveLabels.length - 1],
              ]
                .filter(Boolean)
                .map((a, i) => (
                  <span key={i} className="mono">
                    {a}
                  </span>
                ))}
            </div>
          )}
        </div>

        <div
          className="panel pad"
          style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}
        >
          <div className="panel-head">
            <div className="eyebrow">Win / loss</div>
            <Seg value={wlP} options={periods} onChange={v => setWlP(v as Period)} />
          </div>
          <div
            style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 16,
            }}
          >
            <Donut
              segments={[
                { value: wl.wins, color: 'var(--accent)' },
                { value: wl.losses, color: 'var(--neg)' },
                { value: wl.breakevens, color: 'var(--text-3)' },
              ]}
              label={`${Math.round(wl.winRate)}%`}
              caption="WIN RATE"
              size={160}
            />
            <div className="legend">
              <span>
                <i style={{ background: 'var(--accent)' }} />
                {wl.wins} wins
              </span>
              <span>
                <i style={{ background: 'var(--neg)' }} />
                {wl.losses} losses
              </span>
              {wl.breakevens > 0 && (
                <span>
                  <i style={{ background: 'var(--text-3)' }} />
                  {wl.breakevens} BE
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* POSITIONS */}
      <div className="panel pad">
        <div className="panel-head">
          <h3>
            Open positions{' '}
            <span className="sub" style={{ fontWeight: 400 }}>
              — {positions.length} live
            </span>
          </h3>
          <span className="sub">
            total{' '}
            <span className={`mono ${totalPnl >= 0 ? 'pos' : 'neg'}`} style={{ fontWeight: 600 }}>
              {money(totalPnl)}
            </span>
          </span>
        </div>
        {positions.length === 0 ? (
          <p className="faint" style={{ padding: '12px 0' }}>
            No open positions
          </p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th onClick={() => pos.onSort('sym')}>Symbol{pos.ind('sym')}</th>
                <th onClick={() => pos.onSort('side')}>Side{pos.ind('side')}</th>
                <th className="num" onClick={() => pos.onSort('lot')}>
                  Lot{pos.ind('lot')}
                </th>
                <th className="num">Entry</th>
                <th className="num">Current</th>
                <th className="num" onClick={() => pos.onSort('pnl')}>
                  P&L{pos.ind('pnl')}
                </th>
                <th>Stage</th>
              </tr>
            </thead>
            <tbody>
              {pos.sorted.map(p => (
                <tr key={p.ticket}>
                  <td>
                    <span className="sym">{p.sym}</span>
                  </td>
                  <td>
                    <span className={'tag ' + p.side}>{p.side}</span>
                  </td>
                  <td className="num mono">{p.lot.toFixed(2)}</td>
                  <td className="num mono dim">{p.entry.toFixed(5)}</td>
                  <td className="num mono">{p.cur.toFixed(5)}</td>
                  <td
                    className={'num mono ' + (p.pnl >= 0 ? 'pos' : 'neg')}
                    style={{ fontWeight: 600 }}
                  >
                    {money(p.pnl)}
                  </td>
                  <td>
                    {p.trailing ? (
                      <span className="tag trail">
                        <span className="dot-live" /> trailing
                      </span>
                    ) : (
                      <span className="tag ghost">holding</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* CLOSEST SIGNALS */}
      {signalGroups.length > 0 && (
        <div className="panel pad">
          <div className="panel-head">
            <h3>
              <Icon name="bell" size={17} /> Closest Signals
            </h3>
            {signalGroups.length > 3 && (
              <button className="btn sm ghost" onClick={() => setShowAll(!showAll)}>
                {showAll ? 'Show fewer' : `Show all ${signalGroups.length}`}{' '}
                <Icon
                  name="chevDown"
                  size={14}
                  style={{ transform: showAll ? 'rotate(180deg)' : '', transition: '.2s' }}
                />
              </button>
            )}
          </div>
          <div className="fill-grid">
            {visibleGroups.map(g => (
              <div className={'fill-card' + (g.pct >= 75 ? ' hot' : '')} key={g.signal_id}>
                <div className="top">
                  <span className="sym">{g.sym}</span>
                  <span className={'tag ' + g.side}>{g.side}</span>
                  <span
                    className={
                      'tag ' +
                      (g.action === 'skip'
                        ? 'skipped'
                        : g.action === 'manual'
                          ? 'manual'
                          : g.placed
                            ? 'long'
                            : 'ghost')
                    }
                    style={{ marginLeft: 'auto' }}
                  >
                    {g.action === 'skip'
                      ? 'skipped'
                      : g.action === 'manual'
                        ? 'manual'
                        : g.placed
                          ? 'placed'
                          : 'watching'}
                  </span>
                  <SignalMenu
                    action={g.action}
                    placed={g.placed}
                    onAction={a => handleSignalAction(g.signal_id, a)}
                  />
                </div>
                <ProxMeter pct={g.pct} label={g.dist} />
                <div className="fill-kv">
                  <div className="r">
                    <span className="k">Closest</span>
                    <span className="val mono">{g.closestPrice}</span>
                  </div>
                  <div className="r">
                    <span className="k">Channel</span>
                    <span className="val">{g.channelName}</span>
                  </div>
                  <div className="r">
                    <span className="k">Type</span>
                    <span className="val">{g.signalType}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* RECENT + DAILY */}
      <div className="row">
        <div className="panel pad" style={{ flex: 1.25, minWidth: 0 }}>
          <div className="panel-head">
            <h3>Recent trades</h3>
            <span className="sub">closed · last 5</span>
          </div>
          {recentTradeGroups.length === 0 ? (
            <p className="faint">No recent trades</p>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Limits</th>
                  <th className="num">Total P&L</th>
                  <th className="num">Time</th>
                </tr>
              </thead>
              <tbody>
                {recentTradeGroups.map(g => (
                  <tr key={g.signal_id}>
                    <td>
                      <span className="sym">{g.symbol || '—'}</span>
                    </td>
                    <td>
                      <span className={'tag ' + g.side}>{g.side}</span>
                    </td>
                    <td className="num mono dim">{g.limitCount}</td>
                    <td
                      className="num mono"
                      style={{
                        color:
                          g.outcome === 'breakeven'
                            ? 'var(--text-3)'
                            : g.outcome === 'win'
                              ? 'var(--pos)'
                              : 'var(--neg)',
                        fontWeight: 600,
                      }}
                    >
                      {money(g.totalPnl)}
                    </td>
                    <td className="num t-sub">
                      {g.closedAt
                        ? new Date(g.closedAt).toLocaleTimeString([], {
                            hour: '2-digit',
                            minute: '2-digit',
                          })
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="panel pad" style={{ flex: 1, minWidth: 0 }}>
          <div className="panel-head">
            <h3>Daily P&L</h3>
            <span className="sub">{dailyBars.length} sessions</span>
          </div>
          <Bars data={dailyBars} height={172} />
        </div>
      </div>
    </div>
  )
}
