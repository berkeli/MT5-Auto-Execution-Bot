import { Fragment } from 'react'
import { Icon } from '../../components/Icon'
import { Seg } from '../../components/Seg'
import { CollapsibleSection, partialToTrailing, trailingToPartial } from './shared'
import type { AssetKey, InstrumentOverrideRow, OverrideType, TpRow } from './shared'

interface Props {
  open: boolean
  onToggle: () => void
  tpRows: TpRow[]
  tpTab: 'standard' | OverrideType
  setTpTab: (v: 'standard' | OverrideType) => void
  followServerTp: boolean
  setFollowServerTp: (fn: (v: boolean) => boolean) => void
  followServerBe: boolean
  setFollowServerBe: (fn: (v: boolean) => boolean) => void
  touch: () => void
  instrumentOverrides: Record<AssetKey, InstrumentOverrideRow[]>
  expandedAsset: AssetKey | null
  setExpandedAsset: (v: AssetKey | null) => void
  updateTpStandard: (i: number, field: 'thr' | 'unit' | 'trail' | 'partial', value: string) => void
  updateTpOverride: (
    i: number,
    type: OverrideType,
    field: 'thr' | 'trail' | 'partial',
    value: string
  ) => void
  updateInstrumentOverride: (
    asset: AssetKey,
    i: number,
    field: 'symbol' | 'thr' | 'trail' | 'partial',
    value: string
  ) => void
  updateInstrumentOverrideTyped: (
    asset: AssetKey,
    i: number,
    type: OverrideType,
    field: 'thr' | 'trail' | 'partial',
    value: string
  ) => void
  addInstrumentOverride: (asset: AssetKey) => void
  removeInstrumentOverride: (asset: AssetKey, i: number) => void
}

export function TpSection({
  open,
  onToggle,
  tpRows,
  tpTab,
  setTpTab,
  followServerTp,
  setFollowServerTp,
  followServerBe,
  setFollowServerBe,
  touch,
  instrumentOverrides,
  expandedAsset,
  setExpandedAsset,
  updateTpStandard,
  updateTpOverride,
  updateInstrumentOverride,
  updateInstrumentOverrideTyped,
  addInstrumentOverride,
  removeInstrumentOverride,
}: Props) {
  if (tpRows.length === 0) return null
  // Thresholds go unused in follow-server mode — dim them so the setting's effect is visible.
  const thrStyle = { width: 76, opacity: followServerTp ? 0.4 : 1 }
  return (
    <CollapsibleSection
      head={
        <div
          style={{
            display: 'flex',
            flex: 1,
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
          }}
        >
          <h3>Take-profit &amp; trailing</h3>
          <span className="sub">Use the instrument name as shown in the Discord alert channel</span>
        </div>
      }
      open={open}
      onToggle={onToggle}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginBottom: 18 }}>
        <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={followServerTp}
            onChange={() => {
              setFollowServerTp(v => !v)
              touch()
            }}
            style={{ accentColor: 'var(--accent)', width: 16, height: 16, marginTop: 2 }}
          />
          <span style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>Follow server TP</span>
            <span className="faint" style={{ fontSize: 12.5, maxWidth: 560 }}>
              Take profit when the alert channel calls TP instead of using your own threshold. Trail
              distances and trailing % still apply; the thresholds below are ignored.
            </span>
          </span>
        </label>
        <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={followServerBe}
            onChange={() => {
              setFollowServerBe(v => !v)
              touch()
            }}
            style={{ accentColor: 'var(--accent)', width: 16, height: 16, marginTop: 2 }}
          />
          <span style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>Follow server BE</span>
            <span className="faint" style={{ fontSize: 12.5, maxWidth: 560 }}>
              Close filled positions and cancel remaining limits when the alert channel calls
              breakeven. Turn this off to keep managing the trade locally.
            </span>
          </span>
        </label>
      </div>

      <div style={{ marginBottom: 16 }}>
        <Seg
          accent
          value={tpTab}
          options={[
            { value: 'standard', label: 'Standard' },
            { value: 'scalp', label: 'Scalp' },
            { value: 'toll', label: 'Toll' },
            { value: 'swing', label: 'Swing' },
            { value: 'pa', label: 'PA' },
          ]}
          onChange={v => setTpTab(v as 'standard' | OverrideType)}
        />
      </div>
      {tpTab === 'standard' ? (
        <>
          <p className="faint" style={{ marginTop: 0, marginBottom: 12, fontSize: 12.5 }}>
            Trailing 25% = trail 25% of position, close 75% at TP trigger.
          </p>
          <table className="tbl">
            <thead>
              <tr>
                <th>Asset class</th>
                <th className="num">Threshold</th>
                <th>Unit</th>
                <th className="num">Trail dist.</th>
                <th>Trailing %</th>
              </tr>
            </thead>
            <tbody>
              {tpRows.map((t, i) => {
                const partial = parseInt(t.partial, 10) || 50
                const trailing = partialToTrailing(partial)
                const overrides = instrumentOverrides[t.asset as AssetKey]
                const isExpanded = expandedAsset === t.asset
                return (
                  <Fragment key={t.asset}>
                    <tr>
                      <td
                        onClick={() => setExpandedAsset(isExpanded ? null : (t.asset as AssetKey))}
                        style={{ cursor: 'pointer', userSelect: 'none' }}
                        title={isExpanded ? 'Click to collapse' : 'Click to expand'}
                      >
                        <span className="sym">{t.asset}</span>
                        <Icon
                          name="chevDown"
                          size={14}
                          strokeWidth={2}
                          style={{
                            marginLeft: 6,
                            verticalAlign: 'middle',
                            opacity: 0.55,
                            transform: isExpanded ? 'rotate(180deg)' : 'none',
                            transition: 'transform 120ms ease',
                          }}
                        />
                      </td>
                      <td className="num">
                        <input
                          className="inp num mono"
                          value={t.thr}
                          style={thrStyle}
                          onChange={e => updateTpStandard(i, 'thr', e.target.value)}
                        />
                      </td>
                      <td className="dim">{t.unit}</td>
                      <td className="num">
                        <input
                          className="inp num mono"
                          value={t.trail}
                          style={{ width: 76 }}
                          onChange={e => updateTpStandard(i, 'trail', e.target.value)}
                        />
                      </td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                          <input
                            type="range"
                            min={0}
                            max={100}
                            step={5}
                            value={trailing}
                            onChange={e =>
                              updateTpStandard(
                                i,
                                'partial',
                                String(trailingToPartial(parseInt(e.target.value, 10)))
                              )
                            }
                            style={{ width: 140, accentColor: 'var(--accent)' }}
                          />
                          <span className="mono" style={{ width: 40, fontWeight: 600 }}>
                            {trailing}%
                          </span>
                        </div>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr>
                        <td
                          colSpan={5}
                          style={{
                            background: 'var(--panel-soft, transparent)',
                            padding: '10px 14px',
                          }}
                        >
                          <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>
                            Per-symbol <b>standard</b> overrides for <b>{t.asset}</b> · blank =
                            inherit asset-class value · switch tabs to edit scalp/toll/swing/pa
                          </div>
                          {overrides.length > 0 && (
                            <table className="tbl" style={{ marginBottom: 8 }}>
                              <thead>
                                <tr>
                                  <th>Symbol</th>
                                  <th className="num">Threshold</th>
                                  <th className="num">Trail dist.</th>
                                  <th>Trailing %</th>
                                  <th />
                                </tr>
                              </thead>
                              <tbody>
                                {overrides.map((r, j) => {
                                  const ovPartialSet = r.partial !== ''
                                  const ovPartial = parseInt(r.partial, 10) || 50
                                  const ovTrailing = partialToTrailing(ovPartial)
                                  return (
                                    <tr key={j}>
                                      <td>
                                        <input
                                          className="inp mono"
                                          value={r.symbol}
                                          onChange={e =>
                                            updateInstrumentOverride(
                                              t.asset as AssetKey,
                                              j,
                                              'symbol',
                                              e.target.value
                                            )
                                          }
                                          placeholder="SPX500USD, AMD.NAS, …"
                                          style={{ width: 160 }}
                                        />
                                      </td>
                                      <td className="num">
                                        <input
                                          className="inp num mono"
                                          value={r.thr}
                                          style={{ width: 76 }}
                                          onChange={e =>
                                            updateInstrumentOverride(
                                              t.asset as AssetKey,
                                              j,
                                              'thr',
                                              e.target.value
                                            )
                                          }
                                        />
                                      </td>
                                      <td className="num">
                                        <input
                                          className="inp num mono"
                                          value={r.trail}
                                          style={{ width: 76 }}
                                          onChange={e =>
                                            updateInstrumentOverride(
                                              t.asset as AssetKey,
                                              j,
                                              'trail',
                                              e.target.value
                                            )
                                          }
                                        />
                                      </td>
                                      <td>
                                        <div
                                          style={{
                                            display: 'flex',
                                            alignItems: 'center',
                                            gap: 10,
                                          }}
                                        >
                                          <input
                                            type="range"
                                            min={0}
                                            max={100}
                                            step={5}
                                            value={ovTrailing}
                                            onChange={e =>
                                              updateInstrumentOverride(
                                                t.asset as AssetKey,
                                                j,
                                                'partial',
                                                String(
                                                  trailingToPartial(parseInt(e.target.value, 10))
                                                )
                                              )
                                            }
                                            style={{ width: 140, accentColor: 'var(--accent)' }}
                                          />
                                          <span
                                            className="mono"
                                            style={{ width: 56, fontWeight: 600 }}
                                          >
                                            {ovPartialSet ? (
                                              `${ovTrailing}%`
                                            ) : (
                                              <span className="faint">inherit</span>
                                            )}
                                          </span>
                                          {ovPartialSet && (
                                            <button
                                              className="btn sm ghost"
                                              onClick={() =>
                                                updateInstrumentOverride(
                                                  t.asset as AssetKey,
                                                  j,
                                                  'partial',
                                                  ''
                                                )
                                              }
                                            >
                                              ×
                                            </button>
                                          )}
                                        </div>
                                      </td>
                                      <td style={{ width: 40 }}>
                                        <button
                                          className="btn sm ghost"
                                          onClick={() =>
                                            removeInstrumentOverride(t.asset as AssetKey, j)
                                          }
                                        >
                                          ×
                                        </button>
                                      </td>
                                    </tr>
                                  )
                                })}
                              </tbody>
                            </table>
                          )}
                          <button
                            className="btn sm ghost"
                            onClick={() => addInstrumentOverride(t.asset as AssetKey)}
                          >
                            + Add symbol
                          </button>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </>
      ) : (
        <>
          <p className="faint" style={{ marginTop: 0, marginBottom: 12, fontSize: 12.5 }}>
            {tpTab === 'swing'
              ? 'Leave blank to fall back to 3× the standard threshold. Trailing % left blank inherits from Standard.'
              : 'Leave blank to fall back to the standard asset-class settings.'}
          </p>
          <table className="tbl">
            <thead>
              <tr>
                <th>Asset class</th>
                <th className="num">Threshold</th>
                <th>Unit</th>
                <th className="num">Trail dist.</th>
                <th>Trailing %</th>
              </tr>
            </thead>
            <tbody>
              {tpRows.map((t, i) => {
                const partialSet = t.overrides[tpTab].partial !== ''
                const partialNum = parseInt(t.overrides[tpTab].partial, 10) || 50
                const trailingNum = partialToTrailing(partialNum)
                const overrides = instrumentOverrides[t.asset as AssetKey]
                const isExpanded = expandedAsset === t.asset
                return (
                  <Fragment key={t.asset}>
                    <tr>
                      <td
                        onClick={() => setExpandedAsset(isExpanded ? null : (t.asset as AssetKey))}
                        style={{ cursor: 'pointer', userSelect: 'none' }}
                        title={isExpanded ? 'Click to collapse' : 'Click to expand'}
                      >
                        <span className="sym">{t.asset}</span>
                        <Icon
                          name="chevDown"
                          size={14}
                          strokeWidth={2}
                          style={{
                            marginLeft: 6,
                            verticalAlign: 'middle',
                            opacity: 0.55,
                            transform: isExpanded ? 'rotate(180deg)' : 'none',
                            transition: 'transform 120ms ease',
                          }}
                        />
                      </td>
                      <td className="num">
                        <input
                          className="inp num mono"
                          value={t.overrides[tpTab].thr}
                          style={thrStyle}
                          onChange={e => updateTpOverride(i, tpTab, 'thr', e.target.value)}
                        />
                      </td>
                      <td className="dim">{t.unit}</td>
                      <td className="num">
                        <input
                          className="inp num mono"
                          value={t.overrides[tpTab].trail}
                          style={{ width: 76 }}
                          onChange={e => updateTpOverride(i, tpTab, 'trail', e.target.value)}
                        />
                      </td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                          <input
                            type="range"
                            min={0}
                            max={100}
                            step={5}
                            value={trailingNum}
                            onChange={e =>
                              updateTpOverride(
                                i,
                                tpTab,
                                'partial',
                                String(trailingToPartial(parseInt(e.target.value, 10)))
                              )
                            }
                            style={{ width: 140, accentColor: 'var(--accent)' }}
                          />
                          <span className="mono" style={{ width: 56, fontWeight: 600 }}>
                            {partialSet ? (
                              `${trailingNum}%`
                            ) : (
                              <span className="faint">inherit</span>
                            )}
                          </span>
                          {partialSet && (
                            <button
                              className="btn sm ghost"
                              onClick={() => updateTpOverride(i, tpTab, 'partial', '')}
                            >
                              ×
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr>
                        <td
                          colSpan={5}
                          style={{
                            background: 'var(--panel-soft, transparent)',
                            padding: '10px 14px',
                          }}
                        >
                          <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>
                            Per-symbol <b>{tpTab}</b> overrides for <b>{t.asset}</b> · blank =
                            inherit asset-class {tpTab} value
                          </div>
                          {overrides.length > 0 && (
                            <table className="tbl" style={{ marginBottom: 8 }}>
                              <thead>
                                <tr>
                                  <th>Symbol</th>
                                  <th className="num">Threshold</th>
                                  <th className="num">Trail dist.</th>
                                  <th>Trailing %</th>
                                  <th />
                                </tr>
                              </thead>
                              <tbody>
                                {overrides.map((r, j) => {
                                  const ovPair = r.overrides[tpTab]
                                  const ovPartialSet = ovPair.partial !== ''
                                  const ovPartial = parseInt(ovPair.partial, 10) || 50
                                  const ovTrailing = partialToTrailing(ovPartial)
                                  return (
                                    <tr key={j}>
                                      <td>
                                        <input
                                          className="inp mono"
                                          value={r.symbol}
                                          onChange={e =>
                                            updateInstrumentOverride(
                                              t.asset as AssetKey,
                                              j,
                                              'symbol',
                                              e.target.value
                                            )
                                          }
                                          placeholder="SPX500USD, AMD.NAS, …"
                                          style={{ width: 160 }}
                                        />
                                      </td>
                                      <td className="num">
                                        <input
                                          className="inp num mono"
                                          value={ovPair.thr}
                                          style={{ width: 76 }}
                                          onChange={e =>
                                            updateInstrumentOverrideTyped(
                                              t.asset as AssetKey,
                                              j,
                                              tpTab,
                                              'thr',
                                              e.target.value
                                            )
                                          }
                                        />
                                      </td>
                                      <td className="num">
                                        <input
                                          className="inp num mono"
                                          value={ovPair.trail}
                                          style={{ width: 76 }}
                                          onChange={e =>
                                            updateInstrumentOverrideTyped(
                                              t.asset as AssetKey,
                                              j,
                                              tpTab,
                                              'trail',
                                              e.target.value
                                            )
                                          }
                                        />
                                      </td>
                                      <td>
                                        <div
                                          style={{
                                            display: 'flex',
                                            alignItems: 'center',
                                            gap: 10,
                                          }}
                                        >
                                          <input
                                            type="range"
                                            min={0}
                                            max={100}
                                            step={5}
                                            value={ovTrailing}
                                            onChange={e =>
                                              updateInstrumentOverrideTyped(
                                                t.asset as AssetKey,
                                                j,
                                                tpTab,
                                                'partial',
                                                String(
                                                  trailingToPartial(parseInt(e.target.value, 10))
                                                )
                                              )
                                            }
                                            style={{ width: 140, accentColor: 'var(--accent)' }}
                                          />
                                          <span
                                            className="mono"
                                            style={{ width: 56, fontWeight: 600 }}
                                          >
                                            {ovPartialSet ? (
                                              `${ovTrailing}%`
                                            ) : (
                                              <span className="faint">inherit</span>
                                            )}
                                          </span>
                                          {ovPartialSet && (
                                            <button
                                              className="btn sm ghost"
                                              onClick={() =>
                                                updateInstrumentOverrideTyped(
                                                  t.asset as AssetKey,
                                                  j,
                                                  tpTab,
                                                  'partial',
                                                  ''
                                                )
                                              }
                                            >
                                              ×
                                            </button>
                                          )}
                                        </div>
                                      </td>
                                      <td style={{ width: 40 }}>
                                        <button
                                          className="btn sm ghost"
                                          onClick={() =>
                                            removeInstrumentOverride(t.asset as AssetKey, j)
                                          }
                                        >
                                          ×
                                        </button>
                                      </td>
                                    </tr>
                                  )
                                })}
                              </tbody>
                            </table>
                          )}
                          <button
                            className="btn sm ghost"
                            onClick={() => addInstrumentOverride(t.asset as AssetKey)}
                          >
                            + Add symbol
                          </button>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </>
      )}
    </CollapsibleSection>
  )
}
