// Shared constants, row types, and small components for the Settings sections.
// State ownership stays in SettingsPage; sections receive state + setters as props.
import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { Icon } from '../../components/Icon'

export const ASSET_CLASSES = [
  'forex',
  'forex_jpy',
  'metals',
  'indices',
  'stocks',
  'crypto',
  'oil',
] as const
export type AssetKey = (typeof ASSET_CLASSES)[number]

// Display labels for the asset-class dropdowns.
export const ASSET_CLASS_LABELS: Record<AssetKey, string> = {
  forex: 'Forex',
  forex_jpy: 'Forex (JPY)',
  metals: 'Metals',
  indices: 'Indices',
  stocks: 'Stocks',
  crypto: 'Crypto',
  oil: 'Oil',
}

export type OverrideType = 'scalp' | 'toll' | 'swing' | 'pa'
export const OVERRIDE_TYPES: OverrideType[] = ['scalp', 'toll', 'swing', 'pa']

// Lot-sizing exceptions can target a specific signal type; "all" = every type.
export const LOT_SIGNAL_TYPES: { value: string; label: string }[] = [
  { value: 'all', label: 'All types' },
  { value: 'standard', label: 'Standard' },
  { value: 'scalp', label: 'Scalp' },
  { value: 'swing', label: 'Swing' },
  { value: 'toll', label: 'Toll' },
  { value: 'pa', label: 'PA' },
  { value: '1-1', label: '1-1' },
  { value: 'risky', label: 'Risky' },
]

// Concrete signal types (no "all") — used for the wholesale skip checkboxes.
export const SIGNAL_TYPES = LOT_SIGNAL_TYPES.filter(t => t.value !== 'all')

// Built-in offset-feed instruments. Their feed type and presence are fixed — users
// can only re-map the broker symbol, not change the feed or remove the row. Must
// stay in sync with DEFAULT_OFFSET_INSTRUMENTS in bot/config/settings.py.
export const LOCKED_OFFSET_INSTRUMENTS = new Set([
  'SPX500USD',
  'NAS100USD',
  'BTCUSDT',
  'ETHUSDT',
  'US30USD',
  'US2000USD',
  'USOILSPOT',
  'DE30EUR',
  'UK100GBP',
  'JP225',
  'F40',
  'AUS2000',
  'HK50',
  'CHINA50',
  'CHINAH',
])

// Trailing % is the inverse of partial_close_percent (storage unchanged).
export const partialToTrailing = (p: number) => Math.max(0, Math.min(100, 100 - p))
export const trailingToPartial = (t: number) => Math.max(0, Math.min(100, 100 - t))

export interface OverridePair {
  thr: string
  trail: string
  partial: string // empty string means "inherit from standard"
}

export interface TpRow {
  asset: string
  thr: string
  unit: string
  trail: string
  partial: string // per-asset standard partial close % (default 50)
  overrides: Record<OverrideType, OverridePair>
}

export interface OneToOneOverrideRow {
  asset: string
  value: string
}

export interface SymbolRow {
  db: string
  mt5: string
  feed: boolean
}

export interface LotExceptionRow {
  symbol: string
  channel: string
  signalType: string
  mode: 'risk_percent' | 'fixed' | 'total_lot'
  value: string
}

export interface SuffixRuleRow {
  suffix: string
  classes: AssetKey[]
}

export interface ExcludedTradeRow {
  symbol: string
  signalType: string
}

export interface ExcludedChannelAssetRow {
  channel: string
  assetClass: string
}

export interface InstrumentOverrideRow {
  symbol: string
  // Standard fields — empty string means "inherit asset-class value".
  thr: string
  trail: string
  partial: string // stored as partial_close_percent in config; UI displays trailing
  // Per-signal-type overrides — empty strings mean "inherit".
  overrides: Record<OverrideType, OverridePair>
}

export const FLAT_OVERRIDE_FIELDS = [
  'profit_threshold',
  'trailing_distance',
  'threshold_unit',
  'partial_close_percent',
] as const

export const emptyOverridePairs = (): Record<OverrideType, OverridePair> =>
  Object.fromEntries(OVERRIDE_TYPES.map(t => [t, { thr: '', trail: '', partial: '' }])) as Record<
    OverrideType,
    OverridePair
  >

export type SectionKey = 'lot' | 'tp' | 'oneToOne' | 'risky' | 'symbols' | 'excluded' | 'misc'

// Select-styled dropdown that toggles multiple asset classes. Matches the native
// `.inp.sel` controls; selected rows are filled with the accent tint, classes
// already claimed by another suffix rule are disabled.
export function MultiClassPicker({
  selected,
  disabledClasses,
  onToggle,
}: {
  selected: AssetKey[]
  disabledClasses: Set<AssetKey>
  onToggle: (cls: AssetKey) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!open) return
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])
  const summary = selected.length ? selected.join(', ') : 'Select asset classes'
  return (
    <div
      ref={ref}
      data-classpicker-open={open ? '' : undefined}
      style={{ position: 'relative', width: 280 }}
    >
      <div className="inp sel mono" onClick={() => setOpen(o => !o)} style={{ width: '100%' }}>
        <span
          className={selected.length ? undefined : 'faint'}
          style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {summary}
        </span>
        <Icon
          name="chevDown"
          size={14}
          strokeWidth={2.2}
          style={{
            flexShrink: 0,
            opacity: 0.6,
            transform: open ? 'rotate(180deg)' : 'none',
            transition: 'transform 120ms ease',
          }}
        />
      </div>
      {open && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            left: 0,
            width: '100%',
            background: 'var(--surface)',
            border: '1px solid var(--hairline-strong)',
            borderRadius: 10,
            boxShadow: 'var(--shadow)',
            padding: 4,
            zIndex: 30,
          }}
        >
          {ASSET_CLASSES.map(cls => {
            const active = selected.includes(cls)
            const disabled = !active && disabledClasses.has(cls)
            return (
              <button
                key={cls}
                type="button"
                disabled={disabled}
                onClick={() => onToggle(cls)}
                title={disabled ? 'Already assigned to another suffix' : undefined}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 8,
                  width: '100%',
                  textAlign: 'left',
                  border: 'none',
                  borderRadius: 7,
                  padding: '7px 10px',
                  fontSize: 13,
                  fontFamily: 'var(--mono)',
                  cursor: disabled ? 'not-allowed' : 'pointer',
                  background: active ? 'var(--accent-tint)' : 'transparent',
                  color: active ? 'var(--accent)' : 'var(--text)',
                  opacity: disabled ? 0.45 : 1,
                }}
              >
                {cls}
                {active && <Icon name="check" size={14} strokeWidth={2.6} />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// Collapsible settings panel. The header bar toggles; when collapsed only the
// header shows, so the page stays short once a section is configured.
export function CollapsibleSection({
  head,
  open,
  onToggle,
  children,
}: {
  head: ReactNode
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <div className="panel pad">
      <div
        className="panel-head"
        onClick={onToggle}
        style={{ cursor: 'pointer', userSelect: 'none', marginBottom: open ? 16 : 0 }}
      >
        {head}
        <Icon
          name="chevDown"
          size={18}
          strokeWidth={2.2}
          style={{
            flexShrink: 0,
            opacity: 0.55,
            transform: open ? 'rotate(180deg)' : 'none',
            transition: 'transform 120ms ease',
          }}
        />
      </div>
      {open && children}
    </div>
  )
}
