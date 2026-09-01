import { money } from '../utils/money'
import type { PerformanceBreakdown as Breakdown } from '../utils/stats'

interface Props {
  title: string
  subtitle: string
  rows: Breakdown[]
}

function profitFactor(value: number): string {
  return value === Infinity ? '∞' : value.toFixed(2)
}

export function PerformanceBreakdown({ title, subtitle, rows }: Props) {
  const maxPnl = Math.max(...rows.map(row => Math.abs(row.netPnl)), 1)

  return (
    <section className="panel breakdown-panel">
      <div className="breakdown-head">
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
        <span>{rows.length} groups</span>
      </div>

      {rows.length === 0 ? (
        <div className="breakdown-empty">No closed trades in this slice</div>
      ) : (
        <div className="breakdown-list">
          {rows.map((row, index) => (
            <div className="breakdown-row" key={row.key}>
              <div className="breakdown-rank mono">{String(index + 1).padStart(2, '0')}</div>
              <div className="breakdown-identity">
                <div className="breakdown-name">{row.label}</div>
                <div className="breakdown-track" aria-hidden="true">
                  <span
                    className={row.netPnl >= 0 ? 'positive' : 'negative'}
                    style={{ width: `${Math.max((Math.abs(row.netPnl) / maxPnl) * 100, 3)}%` }}
                  />
                </div>
              </div>
              <div className="breakdown-stat">
                <span>Trades</span>
                <strong className="mono">{row.trades}</strong>
                <small>{row.tradeShare.toFixed(0)}% share</small>
              </div>
              <div className="breakdown-stat">
                <span>Win rate</span>
                <strong className="mono">{row.winRate.toFixed(0)}%</strong>
                <small>
                  {row.wins} W · {row.losses} L{row.breakevens > 0 ? ` · ${row.breakevens} BE` : ''}
                </small>
              </div>
              <div className="breakdown-stat">
                <span>PF / avg</span>
                <strong className="mono">{profitFactor(row.profitFactor)}</strong>
                <small className={row.avgPnl >= 0 ? 'pos' : 'neg'}>{money(row.avgPnl)} avg</small>
              </div>
              <div className="breakdown-pnl">
                <span>Net P&amp;L</span>
                <strong className={`mono ${row.netPnl >= 0 ? 'pos' : 'neg'}`}>
                  {money(row.netPnl)}
                </strong>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
