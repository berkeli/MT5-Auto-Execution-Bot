import { Seg } from '../../components/Seg'
import { CHANNELS } from '../../utils/channels'
import { CollapsibleSection, LOT_SIGNAL_TYPES } from './shared'
import type { LotExceptionRow } from './shared'

interface Props {
  open: boolean
  onToggle: () => void
  lotMode: string
  setLotMode: (v: string) => void
  riskPct: string
  setRiskPct: (v: string) => void
  fixedLotDefault: string
  setFixedLotDefault: (v: string) => void
  totalLotDefault: string
  setTotalLotDefault: (v: string) => void
  maxLot: string
  setMaxLot: (v: string) => void
  skipLimitsAt: string
  setSkipLimitsAt: (v: string) => void
  lotExceptions: LotExceptionRow[]
  updateLotException: (
    i: number,
    field: 'symbol' | 'channel' | 'signalType' | 'mode' | 'value',
    value: string
  ) => void
  addLotException: () => void
  removeLotException: (i: number) => void
  mt5Ok: boolean
  approxLoading: boolean
  approxMsg: { kind: 'success' | 'error'; text: string } | null
  onCalculateApproxLots: () => void
  touch: () => void
}

export function LotSizingSection({
  open,
  onToggle,
  lotMode,
  setLotMode,
  riskPct,
  setRiskPct,
  fixedLotDefault,
  setFixedLotDefault,
  totalLotDefault,
  setTotalLotDefault,
  maxLot,
  setMaxLot,
  skipLimitsAt,
  setSkipLimitsAt,
  lotExceptions,
  updateLotException,
  addLotException,
  removeLotException,
  mt5Ok,
  approxLoading,
  approxMsg,
  onCalculateApproxLots,
  touch,
}: Props) {
  return (
    <CollapsibleSection head={<h3>Lot sizing</h3>} open={open} onToggle={onToggle}>
      <div style={{ display: 'flex', gap: 28, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="field">
          <label>Default mode</label>
          <Seg
            accent
            value={lotMode}
            options={[
              {
                value: 'risk_percent',
                label: 'Risk %',
                title:
                  'Sizes each limit so it risks the chosen percentage of your account balance, based on its distance to the stop loss.',
              },
              {
                value: 'fixed',
                label: 'Fixed lot',
                title:
                  'Places the same lot size on every limit of a signal. More limits means more total volume.',
              },
              {
                value: 'total_lot',
                label: 'Total lot',
                title:
                  "Splits the chosen lot size evenly across a signal's limits. Value 1 across 2 limits places 0.5 on each, so more limits means less per limit.",
              },
            ]}
            onChange={v => {
              setLotMode(v)
              touch()
            }}
          />
        </div>
        {lotMode === 'risk_percent' && (
          <div className="field">
            <label>Risk per signal (%)</label>
            <input
              className="inp num mono"
              value={riskPct}
              onChange={e => {
                setRiskPct(e.target.value)
                touch()
              }}
              style={{ width: 100 }}
            />
          </div>
        )}
        {lotMode === 'fixed' && (
          <div className="field">
            <label>Fixed lot</label>
            <input
              className="inp num mono"
              value={fixedLotDefault}
              onChange={e => {
                setFixedLotDefault(e.target.value)
                touch()
              }}
              style={{ width: 100 }}
            />
          </div>
        )}
        {lotMode === 'total_lot' && (
          <div className="field">
            <label>Total lot</label>
            <input
              className="inp num mono"
              value={totalLotDefault}
              onChange={e => {
                setTotalLotDefault(e.target.value)
                touch()
              }}
              style={{ width: 100 }}
            />
          </div>
        )}
        <div className="field">
          <label>Max lot / order</label>
          <input
            className="inp num mono"
            value={maxLot}
            onChange={e => {
              setMaxLot(e.target.value)
              touch()
            }}
            style={{ width: 100 }}
          />
        </div>
        <div className="field">
          <label>Skip signals with ≥ N limits (0 = off)</label>
          <input
            className="inp num mono"
            value={skipLimitsAt}
            onChange={e => {
              setSkipLimitsAt(e.target.value)
              touch()
            }}
            style={{ width: 100 }}
          />
        </div>
        {(lotMode === 'fixed' || lotMode === 'total_lot') && (
          <div
            className="field"
            style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}
          >
            <label>&nbsp;</label>
            <button
              type="button"
              className="btn"
              onClick={onCalculateApproxLots}
              disabled={approxLoading || !mt5Ok}
              title={
                mt5Ok
                  ? `Suggest ${lotMode === 'total_lot' ? 'total' : 'fixed'} lots per instrument that put a typical signal near 5% account risk, and add them as exceptions below`
                  : 'Connect MT5 first — sizing needs your account balance'
              }
            >
              {approxLoading ? 'Calculating…' : 'Calculate approximate sizes'}
            </button>
          </div>
        )}
      </div>
      {(lotMode === 'fixed' || lotMode === 'total_lot') && approxMsg && (
        <div
          style={{
            marginTop: 10,
            fontSize: 12.5,
            color: approxMsg.kind === 'success' ? 'var(--pos)' : 'var(--neg)',
          }}
        >
          {approxMsg.text}
        </div>
      )}

      <div style={{ height: 1, background: 'var(--hairline)', margin: '20px 0' }} />

      <div className="panel-head" style={{ marginBottom: 6 }}>
        <h3 style={{ fontSize: 14 }}>Exceptions</h3>
        <span className="sub">
          most specific match wins — channel beats symbol beats signal type. A Total value of 0
          auto-sizes from your balance on the next sync
        </span>
      </div>
      {lotExceptions.length > 0 && (
        <table className="tbl" style={{ maxWidth: 860 }}>
          <thead>
            <tr>
              <th>Channel</th>
              <th>Symbol</th>
              <th>Signal type</th>
              <th>Mode</th>
              <th className="num">Value</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {lotExceptions.map((r, i) => (
              <tr key={i}>
                <td>
                  <select
                    className="inp"
                    value={r.channel}
                    onChange={e => updateLotException(i, 'channel', e.target.value)}
                    style={{ width: 140 }}
                  >
                    <option value="">All channels</option>
                    {CHANNELS.map(c => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    className="inp mono"
                    list="broker-symbols"
                    value={r.symbol}
                    onChange={e => updateLotException(i, 'symbol', e.target.value)}
                    placeholder="All symbols"
                    style={{ width: 160 }}
                  />
                </td>
                <td>
                  <select
                    className="inp"
                    value={r.signalType}
                    onChange={e => updateLotException(i, 'signalType', e.target.value)}
                    style={{ width: 130 }}
                  >
                    {LOT_SIGNAL_TYPES.map(t => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <Seg
                    value={r.mode}
                    options={[
                      { value: 'risk_percent', label: 'Risk %' },
                      { value: 'fixed', label: 'Fixed' },
                      { value: 'total_lot', label: 'Total' },
                    ]}
                    onChange={v => updateLotException(i, 'mode', v)}
                  />
                </td>
                <td className="num">
                  <input
                    className="inp num mono"
                    value={r.value}
                    onChange={e => updateLotException(i, 'value', e.target.value)}
                    style={{ width: 88 }}
                  />
                </td>
                <td style={{ width: 40 }}>
                  <button className="btn sm ghost" onClick={() => removeLotException(i)}>
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button className="btn sm ghost" style={{ marginTop: 10 }} onClick={addLotException}>
        + Add exception
      </button>
    </CollapsibleSection>
  )
}
