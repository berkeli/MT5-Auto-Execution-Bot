// Settings container: owns all form state, config load/save serialization, and the
// engine/connection panel. The per-topic section UIs live in ./settings/*.
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Icon } from '../components/Icon'
import {
  startEngine,
  stopEngine,
  shutdownEngine,
  updateConfig,
  resetConfig,
  fetchConfig,
  scanMt5Terminals,
  fetchMt5Symbols,
  fetchNotFoundSymbols,
  fetchApproximateLots,
} from '../api'
import type {
  Config,
  StatusData,
  TPConfig,
  AssetTPConfig,
  ScalpOverrideConfig,
  LotExceptionConfig,
  ExcludedTradeConfig,
  ExcludedChannelAssetConfig,
  SymbolSuffixRule,
} from '../types'
import { detectAssetClass } from '../utils/assetClass'
import { deriveConnStatuses, CONN_CLASS, type ConnState } from '../utils/connStatus'
import {
  ASSET_CLASSES,
  FLAT_OVERRIDE_FIELDS,
  LOCKED_OFFSET_INSTRUMENTS,
  OVERRIDE_TYPES,
  emptyOverridePairs,
} from './settings/shared'
import type {
  AssetKey,
  ExcludedChannelAssetRow,
  ExcludedTradeRow,
  InstrumentOverrideRow,
  LotExceptionRow,
  OneToOneOverrideRow,
  OverridePair,
  OverrideType,
  SectionKey,
  SuffixRuleRow,
  SymbolRow,
  TpRow,
} from './settings/shared'
import { LotSizingSection } from './settings/LotSizingSection'
import { TpSection } from './settings/TpSection'
import { OneToOneSection } from './settings/OneToOneSection'
import { RiskySection } from './settings/RiskySection'
import { SymbolsSection } from './settings/SymbolsSection'
import { ExclusionsSection } from './settings/ExclusionsSection'
import { MiscSection } from './settings/MiscSection'

// Status word for a connection indicator: its own verb when live/error, "idle" otherwise.
const connWord = (state: ConnState, live: string, error: string) =>
  state === 'live' ? live : state === 'error' ? error : 'idle'

interface Props {
  config: Config | null
  status: StatusData | null
  connected: boolean
  onConfigSaved: (config: Config) => void
}

export function SettingsPage({ config, status, connected, onConfigSaved }: Props) {
  const [lotMode, setLotMode] = useState('risk_percent')
  const [riskPct, setRiskPct] = useState('1.0')
  const [fixedLotDefault, setFixedLotDefault] = useState('0.01')
  const [totalLotDefault, setTotalLotDefault] = useState('0.1')
  const [maxLot, setMaxLot] = useState('5.0')
  const [skipLimitsAt, setSkipLimitsAt] = useState('0')
  const [lotExceptions, setLotExceptions] = useState<LotExceptionRow[]>([])
  const [approxLoading, setApproxLoading] = useState(false)
  const [approxMsg, setApproxMsg] = useState<{ kind: 'success' | 'error'; text: string } | null>(
    null
  )
  const [licenseKey, setLicenseKey] = useState('')
  const [mt5TerminalPath, setMt5TerminalPath] = useState('')
  const [tpRows, setTpRows] = useState<TpRow[]>([])
  const [tpTab, setTpTab] = useState<'standard' | OverrideType>('standard')
  const [instrumentOverrides, setInstrumentOverrides] = useState<
    Record<AssetKey, InstrumentOverrideRow[]>
  >(
    () =>
      Object.fromEntries(ASSET_CLASSES.map(a => [a, [] as InstrumentOverrideRow[]])) as Record<
        AssetKey,
        InstrumentOverrideRow[]
      >
  )
  const [expandedAsset, setExpandedAsset] = useState<AssetKey | null>(null)
  const [oneToOneDefault, setOneToOneDefault] = useState('10')
  const [oneToOneRows, setOneToOneRows] = useState<OneToOneOverrideRow[]>([])
  const [riskyTp, setRiskyTp] = useState('4')
  const [riskyTrail, setRiskyTrail] = useState('2')
  const [riskyPartial, setRiskyPartial] = useState('50')
  const [riskySl, setRiskySl] = useState('') // '' = use the signal's DB stop-loss
  const [riskyWindows, setRiskyWindows] = useState<string[]>([])
  const [symbolRows, setSymbolRows] = useState<SymbolRow[]>([])
  const [brokerSymbols, setBrokerSymbols] = useState<string[]>([])
  const [notFoundSymbols, setNotFoundSymbols] = useState<string[]>([])
  const [stockSuffix, setStockSuffix] = useState('-24')
  const [suffixRules, setSuffixRules] = useState<SuffixRuleRow[]>([])
  const [excludedTrades, setExcludedTrades] = useState<ExcludedTradeRow[]>([])
  const [excludedChannelAssets, setExcludedChannelAssets] = useState<ExcludedChannelAssetRow[]>([])
  const [disabledSignalTypes, setDisabledSignalTypes] = useState<string[]>([])
  const [disabledChannels, setDisabledChannels] = useState<string[]>([])
  const [followServerTp, setFollowServerTp] = useState(false)
  const [disableAutoTp, setDisableAutoTp] = useState(false)
  const [volatilityGuard, setVolatilityGuard] = useState(false)
  const [openSections, setOpenSections] = useState<Record<SectionKey, boolean>>(() => {
    const defaults: Record<SectionKey, boolean> = {
      lot: true,
      tp: true,
      oneToOne: true,
      risky: true,
      symbols: true,
      excluded: true,
      misc: true,
    }
    try {
      const stored = localStorage.getItem('settingsOpenSections')
      return stored ? { ...defaults, ...JSON.parse(stored) } : defaults
    } catch {
      return defaults
    }
  })
  const toggleSection = (key: SectionKey) =>
    setOpenSections(prev => {
      const next = { ...prev, [key]: !prev[key] }
      localStorage.setItem('settingsOpenSections', JSON.stringify(next))
      return next
    })
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stopMenuOpen, setStopMenuOpen] = useState(false)
  const stopMenuRef = useRef<HTMLDivElement | null>(null)
  const [pathMenuOpen, setPathMenuOpen] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState<string | null>(null)
  const [terminalPaths, setTerminalPaths] = useState<string[]>([])
  const pathMenuRef = useRef<HTMLDivElement | null>(null)
  const [validateMsg, setValidateMsg] = useState<{
    kind: 'info' | 'success' | 'error'
    text: string
  } | null>(null)

  const touch = () => setDirty(true)

  const initFromConfig = useCallback((cfg: Config) => {
    setLotMode(cfg.lot_sizing.mode)
    setMaxLot(String(cfg.lot_sizing.max_lot_per_order))
    setSkipLimitsAt(String(cfg.lot_sizing.skip_limits_at ?? 0))
    setLicenseKey(cfg.license_key)
    setMt5TerminalPath(cfg.mt5_terminal_path ?? '')

    // Global Risk % default — accept flat number, or "default" key of a dict.
    const rp = cfg.lot_sizing.risk_percent
    if (typeof rp === 'number') {
      setRiskPct(String(rp))
    } else if (rp && typeof rp === 'object') {
      setRiskPct(String((rp as Record<string, number>).default ?? 1.0))
    }

    // Global Fixed lot default — accept flat number, or "default" key of a dict.
    const fl = cfg.lot_sizing.fixed_lot
    if (typeof fl === 'number') {
      setFixedLotDefault(String(fl))
    } else if (fl && typeof fl === 'object') {
      setFixedLotDefault(String((fl as Record<string, number>).default ?? 0.01))
    }

    // Global Total lot default — accept flat number, or "default" key of a dict.
    const tl = cfg.lot_sizing.total_lot
    if (typeof tl === 'number') {
      setTotalLotDefault(String(tl))
    } else if (tl && typeof tl === 'object') {
      setTotalLotDefault(String((tl as Record<string, number>).default ?? 0.1))
    }

    // Load exceptions. Prefer the new `exceptions` field; if absent, migrate
    // any legacy non-`default` keys from risk_percent / fixed_lot dicts.
    const exceptions: LotExceptionRow[] = []
    const seen = new Set<string>()
    if (Array.isArray(cfg.lot_sizing.exceptions)) {
      for (const ex of cfg.lot_sizing.exceptions) {
        exceptions.push({
          symbol: ex.symbol,
          channel: ex.channel ?? '',
          signalType: ex.signal_type || 'all',
          mode: ex.mode,
          value: String(ex.value),
        })
        seen.add(ex.symbol)
      }
    }
    if (rp && typeof rp === 'object') {
      for (const [sym, value] of Object.entries(rp as Record<string, number>)) {
        if (sym === 'default' || seen.has(sym)) continue
        exceptions.push({
          symbol: sym,
          channel: '',
          signalType: 'all',
          mode: 'risk_percent',
          value: String(value),
        })
        seen.add(sym)
      }
    }
    if (fl && typeof fl === 'object') {
      for (const [sym, value] of Object.entries(fl as Record<string, number>)) {
        if (sym === 'default' || seen.has(sym)) continue
        exceptions.push({
          symbol: sym,
          channel: '',
          signalType: 'all',
          mode: 'fixed',
          value: String(value),
        })
        seen.add(sym)
      }
    }
    setLotExceptions(exceptions)

    const tp = cfg.tp_config
    if (tp) {
      const globalPartialFallback = tp.partial_close_percent ?? 50
      const overrideSources: Record<OverrideType, Record<string, ScalpOverrideConfig> | undefined> =
        {
          scalp: tp.scalp_overrides,
          toll: tp.toll_overrides,
          swing: tp.swing_overrides,
          pa: tp.pa_overrides,
        }
      setTpRows(
        ASSET_CLASSES.map(asset => {
          const acfg = tp[asset as AssetKey] as AssetTPConfig | undefined
          const overrides = {} as Record<OverrideType, OverridePair>
          for (const t of OVERRIDE_TYPES) {
            const ov = overrideSources[t]?.[asset]
            overrides[t] = {
              thr: ov ? String(ov.profit_threshold) : '',
              trail: ov ? String(ov.trailing_distance) : '',
              partial:
                ov && ov.partial_close_percent != null ? String(ov.partial_close_percent) : '',
            }
          }
          return {
            asset,
            thr: String(acfg?.profit_threshold ?? ''),
            unit: acfg?.threshold_unit ?? '',
            trail: String(acfg?.trailing_distance ?? ''),
            partial: String(acfg?.partial_close_percent ?? globalPartialFallback),
            overrides,
          }
        })
      )
      const one = tp.one_to_one
      setOneToOneDefault(String(one?.profit_threshold ?? 10))
      setOneToOneRows(
        Object.entries(one?.overrides ?? {}).map(([asset, value]) => ({
          asset,
          value: String(value),
        }))
      )

      const risky = tp.risky
      setRiskyTp(String(risky?.profit_threshold ?? 4))
      setRiskyTrail(String(risky?.trailing_distance ?? 2))
      setRiskyPartial(String(risky?.partial_close_percent ?? 50))
      setRiskySl(risky?.stop_loss == null ? '' : String(risky.stop_loss))
      setRiskyWindows(risky?.disabled_windows ?? ['21:55-23:10', '00:55-02:00', '11:55-14:00'])

      // Group per-symbol instrument overrides by detected asset class.
      const grouped = Object.fromEntries(
        ASSET_CLASSES.map(a => [a, [] as InstrumentOverrideRow[]])
      ) as Record<AssetKey, InstrumentOverrideRow[]>
      for (const [sym, inst] of Object.entries(tp.instrument_overrides ?? {})) {
        const ac = detectAssetClass(sym) as AssetKey
        const i = inst as Record<string, unknown>
        const isFlat = FLAT_OVERRIDE_FIELDS.some(f => f in i)
        const std = (isFlat ? i : ((i.standard as Record<string, unknown>) ?? {})) as Record<
          string,
          unknown
        >
        const overrides = emptyOverridePairs()
        if (!isFlat) {
          for (const t of OVERRIDE_TYPES) {
            const ov = i[t] as Record<string, unknown> | undefined
            if (!ov) continue
            overrides[t] = {
              thr: ov.profit_threshold != null ? String(ov.profit_threshold) : '',
              trail: ov.trailing_distance != null ? String(ov.trailing_distance) : '',
              partial: ov.partial_close_percent != null ? String(ov.partial_close_percent) : '',
            }
          }
        }
        grouped[ac].push({
          symbol: sym,
          thr: std.profit_threshold != null ? String(std.profit_threshold) : '',
          trail: std.trailing_distance != null ? String(std.trailing_distance) : '',
          partial: std.partial_close_percent != null ? String(std.partial_close_percent) : '',
          overrides,
        })
      }
      setInstrumentOverrides(grouped)
    }

    const offsetInst = cfg.offset_instruments ?? []
    const mapped = new Set(Object.keys(cfg.symbol_map ?? {}))
    const rows: SymbolRow[] = Object.entries(cfg.symbol_map ?? {}).map(([db, mt5]) => ({
      db,
      mt5: String(mt5),
      feed: offsetInst.includes(db),
    }))
    // Offset instruments without a symbol_map entry (e.g. JP225, USOILSPOT) still
    // belong in the table so their feed type can be edited and they aren't dropped on
    // save — they map to themselves on the broker.
    for (const db of offsetInst) {
      if (!mapped.has(db)) rows.push({ db, mt5: db, feed: true })
    }
    setSymbolRows(rows)
    setStockSuffix(cfg.stock_suffix ?? '-24')
    const rawSuffixes = cfg.symbol_suffixes ?? []
    const legacyUniversal = (cfg as { universal_suffix?: string }).universal_suffix
    setSuffixRules(
      rawSuffixes.length === 0 && legacyUniversal
        ? [{ suffix: legacyUniversal, classes: [...ASSET_CLASSES] }]
        : rawSuffixes.map(r => ({
            suffix: r.suffix,
            classes: (r.asset_classes as AssetKey[]).filter(c => ASSET_CLASSES.includes(c)),
          }))
    )

    // Excluded trades — new structured list plus any legacy flat excluded_symbols.
    const trades: ExcludedTradeRow[] = (cfg.excluded_trades ?? []).map(t => ({
      symbol: t.symbol,
      signalType: t.signal_type || 'all',
    }))
    for (const sym of cfg.excluded_symbols ?? []) {
      if (!trades.some(t => t.symbol === sym && t.signalType === 'all')) {
        trades.push({ symbol: sym, signalType: 'all' })
      }
    }
    setExcludedTrades(trades)
    setExcludedChannelAssets(
      (cfg.excluded_channel_assets ?? []).map(r => ({
        channel: r.channel || '',
        assetClass: r.asset_class || '',
      }))
    )
    setDisabledSignalTypes(cfg.disabled_signal_types ?? [])
    setDisabledChannels(cfg.disabled_channels ?? [])
    setFollowServerTp(cfg.tp_config?.follow_server_tp ?? true)
    setDisableAutoTp(cfg.disable_auto_tp ?? false)
    setVolatilityGuard(cfg.volatility_guard ?? false)
  }, [])

  useEffect(() => {
    if (config) initFromConfig(config)
  }, [config, initFromConfig])

  // Broker symbol catalogue for the mapping picker. broker_symbols only fills once
  // MT5 is connected and a sync cycle has run, so (re)fetch whenever the connection
  // comes up — fetching only on mount leaves the dropdown empty if Settings is opened
  // before MT5 connects. A transient disconnect keeps the last good list.
  useEffect(() => {
    if (!status?.mt5_connected) return
    fetchMt5Symbols()
      .then(setBrokerSymbols)
      .catch(() => {})
  }, [status?.mt5_connected])

  // Symbols that have a signal but no matching MT5 symbol on this broker. Polled so
  // the list drops entries as soon as a saved mapping resolves on the next cycle.
  useEffect(() => {
    if (!status?.mt5_connected) return
    const load = () =>
      fetchNotFoundSymbols()
        .then(setNotFoundSymbols)
        .catch(() => {})
    load()
    const id = setInterval(load, 15000)
    return () => clearInterval(id)
  }, [status?.mt5_connected])

  const brokerSymbolSet = useMemo(() => new Set(brokerSymbols), [brokerSymbols])
  // Flag a mapping whose target isn't in the broker's catalogue (only once it's loaded).
  const isUnknownSymbol = (sym: string) =>
    brokerSymbolSet.size > 0 && sym.trim().length > 0 && !brokerSymbolSet.has(sym.trim())

  function updateTpStandard(i: number, field: 'thr' | 'unit' | 'trail' | 'partial', value: string) {
    setTpRows(prev => prev.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
    touch()
  }

  function updateTpOverride(
    i: number,
    type: OverrideType,
    field: 'thr' | 'trail' | 'partial',
    value: string
  ) {
    setTpRows(prev =>
      prev.map((r, j) => {
        if (j !== i) return r
        const pair = { ...r.overrides[type], [field]: value }
        return { ...r, overrides: { ...r.overrides, [type]: pair } }
      })
    )
    touch()
  }

  function updateOneToOneRow(i: number, field: 'asset' | 'value', value: string) {
    setOneToOneRows(prev => prev.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
    touch()
  }

  function addOneToOneRow() {
    setOneToOneRows(prev => [...prev, { asset: '', value: '10' }])
    touch()
  }

  function updateRiskyWindow(i: number, value: string) {
    setRiskyWindows(prev => prev.map((w, j) => (j === i ? value : w)))
    touch()
  }

  function addRiskyWindow() {
    setRiskyWindows(prev => [...prev, ''])
    touch()
  }

  function removeRiskyWindow(i: number) {
    setRiskyWindows(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  function removeOneToOneRow(i: number) {
    setOneToOneRows(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  function updateSymbolRow(i: number, field: 'db' | 'mt5', value: string) {
    setSymbolRows(prev => prev.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
    touch()
  }

  function updateSymbolFeed(i: number, feed: boolean) {
    setSymbolRows(prev => prev.map((r, j) => (j === i ? { ...r, feed } : r)))
    touch()
  }

  function addSymbolRow() {
    setSymbolRows(prev => [...prev, { db: '', mt5: '', feed: false }])
    touch()
  }

  function removeSymbolRow(i: number) {
    setSymbolRows(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  function addSuffixRule() {
    setSuffixRules(prev => [...prev, { suffix: '', classes: [] }])
    touch()
  }

  function removeSuffixRule(i: number) {
    setSuffixRules(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  function updateSuffixRuleSuffix(i: number, value: string) {
    setSuffixRules(prev => prev.map((r, j) => (j === i ? { ...r, suffix: value } : r)))
    touch()
  }

  // Toggle a class on a rule; a class assigned elsewhere is blocked in the UI, so
  // rules stay conflict-free (the backend rejects conflicts on save too).
  function toggleSuffixRuleClass(i: number, cls: AssetKey) {
    setSuffixRules(prev =>
      prev.map((r, j) => {
        if (j !== i) return r
        const has = r.classes.includes(cls)
        return { ...r, classes: has ? r.classes.filter(c => c !== cls) : [...r.classes, cls] }
      })
    )
    touch()
  }

  function buildSuffixRules(): SymbolSuffixRule[] {
    return suffixRules
      .filter(r => r.suffix.trim() && r.classes.length > 0)
      .map(r => ({ suffix: r.suffix.trim(), asset_classes: r.classes }))
  }

  function updateLotException(
    i: number,
    field: 'symbol' | 'channel' | 'signalType' | 'mode' | 'value',
    value: string
  ) {
    setLotExceptions(prev =>
      prev.map((r, j) => {
        if (j !== i) return r
        if (field === 'mode') return { ...r, mode: value as 'risk_percent' | 'fixed' | 'total_lot' }
        return { ...r, [field]: value }
      })
    )
    touch()
  }

  function addLotException() {
    setLotExceptions(prev => [
      ...prev,
      { symbol: '', channel: '', signalType: 'all', mode: 'risk_percent', value: '1.0' },
    ])
    touch()
  }

  function removeLotException(i: number) {
    setLotExceptions(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  function buildLotExceptions(): LotExceptionConfig[] {
    return lotExceptions
      .filter(r => r.value.trim() !== '')
      .map(r => ({
        symbol: r.symbol.trim(),
        channel: r.channel,
        signal_type: r.signalType,
        mode: r.mode,
        value: parseFloat(r.value) || 0,
      }))
  }

  // Fetch per-instrument lots sized for ~5% average risk and upsert them into the
  // exceptions table (by symbol + signal type), leaving unrelated rows untouched. Uses
  // the active global mode so total-lot mode gets total-per-signal lots, not per-limit.
  async function handleCalculateApproxLots() {
    const mode = lotMode === 'total_lot' ? 'total_lot' : 'fixed'
    setApproxLoading(true)
    setApproxMsg(null)
    try {
      const data = await fetchApproximateLots(mode)
      if (data.exceptions.length === 0) {
        setApproxMsg({ kind: 'error', text: 'No supported instruments found on this broker.' })
        return
      }
      setLotExceptions(prev => {
        const next = [...prev]
        for (const ex of data.exceptions) {
          const value = String(ex.value)
          const channel = ex.channel ?? ''
          const i = next.findIndex(
            r =>
              r.symbol.trim() === ex.symbol &&
              r.channel === channel &&
              r.signalType === ex.signal_type
          )
          if (i >= 0) next[i] = { ...next[i], mode: ex.mode, value }
          else
            next.push({
              symbol: ex.symbol,
              channel,
              signalType: ex.signal_type,
              mode: ex.mode,
              value,
            })
        }
        return next
      })
      touch()
      const bal = data.balance.toLocaleString(undefined, { maximumFractionDigits: 0 })
      const label = mode === 'total_lot' ? 'total lots' : 'fixed lots'
      setApproxMsg({
        kind: 'success',
        text: `Set ${data.exceptions.length} ${label} for ~5% avg risk on ${data.currency ?? ''} ${bal} balance. Review and save.`,
      })
    } catch (e) {
      setApproxMsg({ kind: 'error', text: e instanceof Error ? e.message : 'Calculation failed' })
    } finally {
      setApproxLoading(false)
    }
  }

  function updateExcludedTrade(i: number, field: 'symbol' | 'signalType', value: string) {
    setExcludedTrades(prev => prev.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
    touch()
  }

  function addExcludedTrade() {
    setExcludedTrades(prev => [...prev, { symbol: '', signalType: 'all' }])
    touch()
  }

  function removeExcludedTrade(i: number) {
    setExcludedTrades(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  function buildExcludedTrades(): ExcludedTradeConfig[] {
    return excludedTrades
      .filter(r => r.symbol.trim())
      .map(r => ({ symbol: r.symbol.trim(), signal_type: r.signalType }))
  }

  function updateExcludedChannelAsset(i: number, field: 'channel' | 'assetClass', value: string) {
    setExcludedChannelAssets(prev => prev.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
    touch()
  }

  function addExcludedChannelAsset() {
    setExcludedChannelAssets(prev => [...prev, { channel: '', assetClass: '' }])
    touch()
  }

  function removeExcludedChannelAsset(i: number) {
    setExcludedChannelAssets(prev => prev.filter((_, j) => j !== i))
    touch()
  }

  // Drop rows with no constraint (both wildcards) — they'd exclude every trade.
  function buildExcludedChannelAssets(): ExcludedChannelAssetConfig[] {
    return excludedChannelAssets
      .filter(r => r.channel || r.assetClass)
      .map(r => ({ channel: r.channel, asset_class: r.assetClass }))
  }

  // Checkboxes store the *disabled* set (default empty = all enabled), so a toggle
  // adds/removes the item from that list.
  function toggleDisabled(list: string[], setList: (v: string[]) => void, key: string) {
    setList(list.includes(key) ? list.filter(k => k !== key) : [...list, key])
    touch()
  }

  function updateInstrumentOverride(
    asset: AssetKey,
    i: number,
    field: 'symbol' | 'thr' | 'trail' | 'partial',
    value: string
  ) {
    setInstrumentOverrides(prev => ({
      ...prev,
      [asset]: prev[asset].map((r, j) => (j === i ? { ...r, [field]: value } : r)),
    }))
    touch()
  }

  function addInstrumentOverride(asset: AssetKey) {
    setInstrumentOverrides(prev => ({
      ...prev,
      [asset]: [
        ...prev[asset],
        { symbol: '', thr: '', trail: '', partial: '', overrides: emptyOverridePairs() },
      ],
    }))
    touch()
  }

  function updateInstrumentOverrideTyped(
    asset: AssetKey,
    i: number,
    type: OverrideType,
    field: 'thr' | 'trail' | 'partial',
    value: string
  ) {
    setInstrumentOverrides(prev => ({
      ...prev,
      [asset]: prev[asset].map((r, j) => {
        if (j !== i) return r
        const pair = { ...r.overrides[type], [field]: value }
        return { ...r, overrides: { ...r.overrides, [type]: pair } }
      }),
    }))
    touch()
  }

  function removeInstrumentOverride(asset: AssetKey, i: number) {
    setInstrumentOverrides(prev => ({
      ...prev,
      [asset]: prev[asset].filter((_, j) => j !== i),
    }))
    touch()
  }

  function buildInstrumentOverrides(): Record<string, Record<string, unknown>> {
    const out: Record<string, Record<string, unknown>> = {}
    for (const asset of ASSET_CLASSES) {
      for (const row of instrumentOverrides[asset]) {
        const sym = row.symbol.trim()
        if (!sym) continue

        const std: Record<string, unknown> = {}
        if (row.thr !== '') std.profit_threshold = parseFloat(row.thr) || 0
        if (row.trail !== '') std.trailing_distance = parseFloat(row.trail) || 0
        if (row.partial !== '') std.partial_close_percent = parseInt(row.partial, 10) || 0

        const perType: Record<string, Record<string, unknown>> = {}
        for (const t of OVERRIDE_TYPES) {
          const ov = row.overrides[t]
          const block: Record<string, unknown> = {}
          if (ov.thr !== '') block.profit_threshold = parseFloat(ov.thr) || 0
          if (ov.trail !== '') block.trailing_distance = parseFloat(ov.trail) || 0
          if (ov.partial !== '') block.partial_close_percent = parseInt(ov.partial, 10) || 0
          if (Object.keys(block).length > 0) perType[t] = block
        }

        const hasStd = Object.keys(std).length > 0
        const hasOverrides = Object.keys(perType).length > 0
        if (!hasStd && !hasOverrides) continue

        if (hasOverrides) {
          const entry: Record<string, unknown> = { ...perType }
          if (hasStd) entry.standard = std
          out[sym] = entry
        } else {
          out[sym] = std
        }
      }
    }
    return out
  }

  function buildTpConfig(): TPConfig {
    const assetEntries = Object.fromEntries(
      tpRows.map(row => [
        row.asset,
        {
          profit_threshold: parseFloat(row.thr) || 0,
          threshold_unit: row.unit,
          trailing_distance: parseFloat(row.trail) || 0,
          partial_close_percent: parseInt(row.partial, 10) || 50,
        },
      ])
    )
    const overrideMaps = {} as Record<
      `${OverrideType}_overrides`,
      Record<string, ScalpOverrideConfig>
    >
    for (const t of OVERRIDE_TYPES) {
      overrideMaps[`${t}_overrides`] = Object.fromEntries(
        tpRows
          .filter(
            row =>
              row.overrides[t].thr !== '' ||
              row.overrides[t].trail !== '' ||
              row.overrides[t].partial !== ''
          )
          .map(row => {
            const entry: ScalpOverrideConfig = {
              profit_threshold: parseFloat(row.overrides[t].thr) || 0,
              trailing_distance: parseFloat(row.overrides[t].trail) || 0,
            }
            if (row.overrides[t].partial !== '') {
              entry.partial_close_percent = parseInt(row.overrides[t].partial, 10) || 50
            }
            return [row.asset, entry]
          })
      )
    }
    const oneToOne = {
      profit_threshold: parseFloat(oneToOneDefault) || 10,
      overrides: Object.fromEntries(
        oneToOneRows
          .filter(r => r.asset.trim())
          .map(r => [r.asset.trim(), parseFloat(r.value) || 0])
      ),
    }
    const slParsed = parseFloat(riskySl)
    const risky = {
      profit_threshold: parseFloat(riskyTp) || 4,
      threshold_unit: 'dollars',
      trailing_distance: parseFloat(riskyTrail) || 0,
      partial_close_percent: parseInt(riskyPartial, 10) || 50,
      stop_loss: riskySl.trim() === '' || isNaN(slParsed) ? null : slParsed,
      disabled_windows: riskyWindows.map(w => w.trim()).filter(Boolean),
      overrides: config!.tp_config.risky?.overrides ?? {},
    }
    return {
      ...config!.tp_config,
      ...assetEntries,
      ...overrideMaps,
      follow_server_tp: followServerTp,
      one_to_one: oneToOne,
      risky,
      instrument_overrides: buildInstrumentOverrides(),
    } as TPConfig
  }

  function buildSymbolMap(): Record<string, string> {
    // Skip identity rows (mt5 === db): they're no-op mappings, used only to surface an
    // offset-only instrument in the table. Their feed type is persisted via
    // offset_instruments, not symbol_map.
    return Object.fromEntries(
      symbolRows
        .filter(r => r.db.trim() && r.mt5.trim() && r.db.trim() !== r.mt5.trim())
        .map(r => [r.db.trim(), r.mt5.trim()])
    )
  }

  function buildOffsetInstruments(): string[] {
    // Locked built-in defaults are always offset feeds regardless of the (disabled) toggle.
    return symbolRows
      .filter(r => r.db.trim() && (r.feed || LOCKED_OFFSET_INSTRUMENTS.has(r.db.trim())))
      .map(r => r.db.trim())
  }

  async function handleSave() {
    if (!config) return
    setSaving(true)
    setError(null)
    try {
      const updated: Config = {
        ...config,
        license_key: licenseKey,
        mt5_terminal_path: mt5TerminalPath,
        lot_sizing: {
          mode: lotMode,
          risk_percent: parseFloat(riskPct) || 1.0,
          fixed_lot: parseFloat(fixedLotDefault) || 0.01,
          total_lot: parseFloat(totalLotDefault) || 0.1,
          max_lot_per_order: parseFloat(maxLot) || 5.0,
          skip_limits_at: skipLimitsAt === '' ? 0 : Math.max(0, parseInt(skipLimitsAt, 10) || 0),
          exceptions: buildLotExceptions(),
        },
        tp_config: buildTpConfig(),
        symbol_map: buildSymbolMap(),
        offset_instruments: buildOffsetInstruments(),
        stock_suffix: stockSuffix,
        symbol_suffixes: buildSuffixRules(),
        excluded_trades: buildExcludedTrades(),
        excluded_channel_assets: buildExcludedChannelAssets(),
        excluded_symbols: [], // migrated into excluded_trades
        disabled_signal_types: disabledSignalTypes,
        disabled_channels: disabledChannels,
        disable_auto_tp: disableAutoTp,
        volatility_guard: volatilityGuard,
      }
      await updateConfig(updated)
      onConfigSaved(updated)
      setSaving(false)
      setDirty(false)
      setToast(true)
      setTimeout(() => setToast(false), 2600)
    } catch (e) {
      setSaving(false)
      setError(e instanceof Error ? e.message : 'Save failed')
    }
  }

  function handleDiscard() {
    if (config) {
      initFromConfig(config)
      setDirty(false)
    }
  }

  async function handleRestoreDefaults() {
    if (
      !window.confirm(
        'Restore all settings to the bot defaults? Your license key, terminal path, and symbol mappings are kept. This cannot be undone.'
      )
    ) {
      return
    }
    setSaving(true)
    setError(null)
    try {
      await resetConfig()
      const fresh = await fetchConfig()
      onConfigSaved(fresh)
      initFromConfig(fresh)
      setSaving(false)
      setDirty(false)
      setToast(true)
      setTimeout(() => setToast(false), 2600)
    } catch (e) {
      setSaving(false)
      setError(e instanceof Error ? e.message : 'Restore defaults failed')
    }
  }

  async function handleValidate() {
    if (!config) return
    setValidateMsg({ kind: 'info', text: 'Saving — waiting for license check…' })
    try {
      const updated: Config = {
        ...config,
        license_key: licenseKey,
        mt5_terminal_path: mt5TerminalPath,
      }
      await updateConfig(updated)
      onConfigSaved(updated)
    } catch (e) {
      setValidateMsg({
        kind: 'error',
        text: 'Save failed: ' + (e instanceof Error ? e.message : 'unknown'),
      })
    }
  }

  async function handleStart() {
    setBusy(true)
    setError(null)
    try {
      await startEngine()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally {
      setBusy(false)
    }
  }

  async function handlePauseTrading() {
    setBusy(true)
    setError(null)
    setStopMenuOpen(false)
    try {
      await stopEngine()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleShutdown() {
    setStopMenuOpen(false)
    try {
      await shutdownEngine()
    } catch {
      /* connection will drop */
    }
  }

  useEffect(() => {
    if (!stopMenuOpen) return
    function onClick(e: MouseEvent) {
      if (stopMenuRef.current && !stopMenuRef.current.contains(e.target as Node)) {
        setStopMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [stopMenuOpen])

  useEffect(() => {
    if (!pathMenuOpen) return
    function onClick(e: MouseEvent) {
      if (pathMenuRef.current && !pathMenuRef.current.contains(e.target as Node)) {
        setPathMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [pathMenuOpen])

  async function handleScanTerminals() {
    setPathMenuOpen(true)
    setScanning(true)
    setScanError(null)
    try {
      const paths = await scanMt5Terminals()
      setTerminalPaths(paths)
    } catch (e) {
      setScanError(e instanceof Error ? e.message : 'Scan failed')
      setTerminalPaths([])
    } finally {
      setScanning(false)
    }
  }

  function pickTerminalPath(p: string) {
    setMt5TerminalPath(p)
    setPathMenuOpen(false)
    touch()
  }

  const isActive = status?.trading_active ?? false
  const mt5Ok = status?.mt5_connected ?? false
  const conns = deriveConnStatuses(status, connected)

  // Once a Save & validate is pending, watch the status feed for the engine's
  // verdict on both the MT5 terminal and the license. The engine reconnects MT5
  // and re-validates asynchronously after its next sync cycle; surface whichever
  // is failing instead of leaving the toast stuck on "waiting".
  useEffect(() => {
    if (!validateMsg || validateMsg.kind !== 'info') return
    // MT5 first — the license can't validate without the terminal (it needs the account).
    if (conns.mt5.state === 'error') {
      setValidateMsg({ kind: 'error', text: `MT5 terminal not connected: ${conns.mt5.detail}` })
      return
    }
    if (conns.mt5.state === 'live' && conns.license.state === 'live') {
      setValidateMsg({ kind: 'success', text: 'MT5 connected and license valid — engine running.' })
      return
    }
    // A confirmed license rejection (wrong key/account/expired) — report right away.
    if (conns.license.state === 'error') {
      setValidateMsg({
        kind: 'error',
        text: status?.license_message || 'License rejected — check the key and MT5 account.',
      })
      return
    }
    const t = setTimeout(() => {
      setValidateMsg({
        kind: 'error',
        text:
          status?.license_message ||
          'Could not validate. Check the license key and MT5 terminal path, then retry.',
      })
    }, 30000)
    return () => clearTimeout(t)
  }, [conns.mt5.state, conns.mt5.detail, conns.license.state, validateMsg, status?.license_message])

  useEffect(() => {
    if (!validateMsg || validateMsg.kind === 'info') return
    const t = setTimeout(() => setValidateMsg(null), 5000)
    return () => clearTimeout(t)
  }, [validateMsg])

  return (
    <div className="page">
      <datalist id="broker-symbols">
        {brokerSymbols.map(s => (
          <option key={s} value={s} />
        ))}
      </datalist>
      <div>
        <div className="eyebrow">Configuration</div>
        <h2 style={{ margin: '4px 0 0', fontSize: 24, fontWeight: 700, letterSpacing: '-0.01em' }}>
          Settings
        </h2>
      </div>

      {/* ENGINE & CONNECTION */}
      <div className="panel pad">
        <div className="panel-head">
          <h3>Engine &amp; connection</h3>
        </div>
        <div style={{ display: 'flex', gap: 28, alignItems: 'center', flexWrap: 'wrap' }}>
          <div className={`conn ${CONN_CLASS[conns.mt5.state]}`} title={conns.mt5.detail ?? ''}>
            <span className="d" /> MT5 {connWord(conns.mt5.state, 'connected', 'error')}
            {conns.mt5.state === 'error' && conns.mt5.detail && (
              <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>
                · {conns.mt5.detail}
              </span>
            )}
          </div>
          <div
            className={`conn ${CONN_CLASS[conns.database.state]}`}
            title={conns.database.detail ?? ''}
          >
            <span className="d" /> Database {connWord(conns.database.state, 'connected', 'error')}
            {conns.database.state === 'error' && conns.database.detail && (
              <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>
                · {conns.database.detail}
              </span>
            )}
          </div>
          <div
            className={`conn ${CONN_CLASS[conns.license.state]}`}
            title={conns.license.detail ?? ''}
          >
            <span className="d" /> License {connWord(conns.license.state, 'valid', 'invalid')}
            {conns.license.state === 'error' && conns.license.detail && (
              <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>
                · {conns.license.detail}
              </span>
            )}
          </div>
          <div style={{ flex: 1 }} />
          {isActive ? (
            <div ref={stopMenuRef} style={{ position: 'relative' }}>
              <button className="btn" onClick={() => setStopMenuOpen(o => !o)} disabled={busy}>
                <Icon name="power" size={14} strokeWidth={2.2} /> Stop engine ▾
              </button>
              {stopMenuOpen && (
                <div
                  style={{
                    position: 'absolute',
                    top: 'calc(100% + 4px)',
                    right: 0,
                    minWidth: 200,
                    background: 'var(--bg-panel, #fff)',
                    border: '1px solid var(--hairline, rgba(0,0,0,0.12))',
                    borderRadius: 8,
                    boxShadow: '0 6px 18px rgba(0,0,0,0.12)',
                    padding: 4,
                    zIndex: 20,
                  }}
                >
                  <button
                    className="btn"
                    style={{
                      width: '100%',
                      justifyContent: 'flex-start',
                      background: 'transparent',
                      border: 'none',
                    }}
                    onClick={handlePauseTrading}
                    title="Freeze new placements but keep TP/trailing running for open positions"
                  >
                    Pause trading
                  </button>
                  <button
                    className="btn danger"
                    style={{
                      width: '100%',
                      justifyContent: 'flex-start',
                      background: 'transparent',
                      border: 'none',
                    }}
                    onClick={handleShutdown}
                    title="Stop all loops and exit the bot process"
                  >
                    Full shutdown
                  </button>
                </div>
              )}
            </div>
          ) : (
            <>
              <button className="btn" onClick={handleStart} disabled={busy}>
                <Icon name="power" size={14} strokeWidth={2.2} /> Start engine
              </button>
              <button className="btn danger" onClick={handleShutdown}>
                <Icon name="power" size={14} strokeWidth={2.2} /> Full shutdown
              </button>
            </>
          )}
        </div>
        <div style={{ height: 1, background: 'var(--hairline)', margin: '20px 0' }} />
        <div style={{ display: 'flex', gap: 14, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div className="field">
            <label>License key</label>
            <input
              className="inp mono"
              value={licenseKey}
              onChange={e => {
                setLicenseKey(e.target.value)
                touch()
              }}
              style={{ width: 280 }}
            />
          </div>
          <div className="field">
            <label>MT5 terminal path (optional)</label>
            <div
              ref={pathMenuRef}
              style={{ display: 'flex', gap: 6, alignItems: 'center', position: 'relative' }}
            >
              <input
                className="inp mono"
                value={mt5TerminalPath}
                onChange={e => {
                  setMt5TerminalPath(e.target.value)
                  touch()
                }}
                placeholder="e.g. C:\Program Files\MetaTrader 5\terminal64.exe"
                style={{ width: 420 }}
              />
              <button
                type="button"
                className="btn sm ghost"
                onClick={handleScanTerminals}
                disabled={scanning}
                title="Search this PC for installed MT5 terminals"
                style={{ padding: '6px 10px' }}
              >
                <Icon name="folder" size={15} strokeWidth={2} />
              </button>
              {pathMenuOpen && (
                <div
                  style={{
                    position: 'absolute',
                    top: 'calc(100% + 4px)',
                    left: 0,
                    minWidth: 420,
                    maxWidth: 560,
                    background: 'var(--surface)',
                    border: '1px solid var(--hairline-strong)',
                    borderRadius: 10,
                    boxShadow: 'var(--shadow)',
                    padding: 4,
                    zIndex: 30,
                    maxHeight: 240,
                    overflowY: 'auto',
                  }}
                >
                  {scanning ? (
                    <div className="faint" style={{ padding: '8px 12px', fontSize: 12.5 }}>
                      Searching…
                    </div>
                  ) : scanError ? (
                    <div
                      style={{
                        padding: '8px 12px',
                        fontSize: 12.5,
                        color: 'var(--neg)',
                      }}
                    >
                      {scanError}
                    </div>
                  ) : terminalPaths.length === 0 ? (
                    <div className="faint" style={{ padding: '8px 12px', fontSize: 12.5 }}>
                      No MT5 terminals found
                    </div>
                  ) : (
                    terminalPaths.map(p => (
                      <button
                        key={p}
                        type="button"
                        className="btn sm ghost mono"
                        onClick={() => pickTerminalPath(p)}
                        style={{
                          width: '100%',
                          justifyContent: 'flex-start',
                          border: 'none',
                          textAlign: 'left',
                          fontSize: 12.5,
                          padding: '6px 10px',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          display: 'block',
                        }}
                        title={p}
                      >
                        {p}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          </div>
          <button className="btn" onClick={handleValidate}>
            Save &amp; validate
          </button>
          {validateMsg && (
            <div
              style={{
                fontSize: 13,
                padding: '6px 12px',
                borderRadius: 6,
                color:
                  validateMsg.kind === 'success'
                    ? '#1f7a1f'
                    : validateMsg.kind === 'error'
                      ? '#a32020'
                      : 'var(--ink-muted, #666)',
                background:
                  validateMsg.kind === 'success'
                    ? 'rgba(31,122,31,0.08)'
                    : validateMsg.kind === 'error'
                      ? 'rgba(163,32,32,0.08)'
                      : 'rgba(0,0,0,0.04)',
                border:
                  '1px solid ' +
                  (validateMsg.kind === 'success'
                    ? 'rgba(31,122,31,0.25)'
                    : validateMsg.kind === 'error'
                      ? 'rgba(163,32,32,0.25)'
                      : 'rgba(0,0,0,0.08)'),
                alignSelf: 'center',
              }}
            >
              {validateMsg.text}
            </div>
          )}
        </div>
      </div>

      <LotSizingSection
        open={openSections.lot}
        onToggle={() => toggleSection('lot')}
        lotMode={lotMode}
        setLotMode={setLotMode}
        riskPct={riskPct}
        setRiskPct={setRiskPct}
        fixedLotDefault={fixedLotDefault}
        setFixedLotDefault={setFixedLotDefault}
        totalLotDefault={totalLotDefault}
        setTotalLotDefault={setTotalLotDefault}
        maxLot={maxLot}
        setMaxLot={setMaxLot}
        skipLimitsAt={skipLimitsAt}
        setSkipLimitsAt={setSkipLimitsAt}
        lotExceptions={lotExceptions}
        updateLotException={updateLotException}
        addLotException={addLotException}
        removeLotException={removeLotException}
        mt5Ok={mt5Ok}
        approxLoading={approxLoading}
        approxMsg={approxMsg}
        onCalculateApproxLots={handleCalculateApproxLots}
        touch={touch}
      />

      <TpSection
        open={openSections.tp}
        onToggle={() => toggleSection('tp')}
        tpRows={tpRows}
        tpTab={tpTab}
        setTpTab={setTpTab}
        followServerTp={followServerTp}
        setFollowServerTp={setFollowServerTp}
        touch={touch}
        instrumentOverrides={instrumentOverrides}
        expandedAsset={expandedAsset}
        setExpandedAsset={setExpandedAsset}
        updateTpStandard={updateTpStandard}
        updateTpOverride={updateTpOverride}
        updateInstrumentOverride={updateInstrumentOverride}
        updateInstrumentOverrideTyped={updateInstrumentOverrideTyped}
        addInstrumentOverride={addInstrumentOverride}
        removeInstrumentOverride={removeInstrumentOverride}
      />

      <OneToOneSection
        open={openSections.oneToOne}
        onToggle={() => toggleSection('oneToOne')}
        oneToOneDefault={oneToOneDefault}
        setOneToOneDefault={setOneToOneDefault}
        oneToOneRows={oneToOneRows}
        updateOneToOneRow={updateOneToOneRow}
        addOneToOneRow={addOneToOneRow}
        removeOneToOneRow={removeOneToOneRow}
        touch={touch}
      />

      <RiskySection
        open={openSections.risky}
        onToggle={() => toggleSection('risky')}
        riskyTp={riskyTp}
        setRiskyTp={setRiskyTp}
        riskyTrail={riskyTrail}
        setRiskyTrail={setRiskyTrail}
        riskyPartial={riskyPartial}
        setRiskyPartial={setRiskyPartial}
        riskySl={riskySl}
        setRiskySl={setRiskySl}
        riskyWindows={riskyWindows}
        updateRiskyWindow={updateRiskyWindow}
        addRiskyWindow={addRiskyWindow}
        removeRiskyWindow={removeRiskyWindow}
        touch={touch}
      />

      <SymbolsSection
        open={openSections.symbols}
        onToggle={() => toggleSection('symbols')}
        symbolRows={symbolRows}
        updateSymbolRow={updateSymbolRow}
        updateSymbolFeed={updateSymbolFeed}
        addSymbolRow={addSymbolRow}
        removeSymbolRow={removeSymbolRow}
        isUnknownSymbol={isUnknownSymbol}
        stockSuffix={stockSuffix}
        setStockSuffix={setStockSuffix}
        suffixRules={suffixRules}
        updateSuffixRuleSuffix={updateSuffixRuleSuffix}
        toggleSuffixRuleClass={toggleSuffixRuleClass}
        addSuffixRule={addSuffixRule}
        removeSuffixRule={removeSuffixRule}
        touch={touch}
      />

      {/* NOT FOUND SYMBOLS */}
      <div className="panel pad">
        <div className="panel-head">
          <h3>Not found symbols</h3>
          <span className="sub">
            signals whose instrument has no matching MT5 symbol on this broker · add a mapping above
            to resolve
          </span>
        </div>
        {notFoundSymbols.length > 0 ? (
          <p className="mono" style={{ fontSize: 13, lineHeight: 1.7, margin: 0 }}>
            {notFoundSymbols.join(', ')}
          </p>
        ) : (
          <p className="faint" style={{ fontSize: 13, margin: 0 }}>
            None — every signalled instrument maps to a symbol on this broker.
          </p>
        )}
      </div>

      <ExclusionsSection
        open={openSections.excluded}
        onToggle={() => toggleSection('excluded')}
        excludedTrades={excludedTrades}
        updateExcludedTrade={updateExcludedTrade}
        addExcludedTrade={addExcludedTrade}
        removeExcludedTrade={removeExcludedTrade}
        excludedChannelAssets={excludedChannelAssets}
        updateExcludedChannelAsset={updateExcludedChannelAsset}
        addExcludedChannelAsset={addExcludedChannelAsset}
        removeExcludedChannelAsset={removeExcludedChannelAsset}
        disabledSignalTypes={disabledSignalTypes}
        disabledChannels={disabledChannels}
        toggleDisabled={toggleDisabled}
        setDisabledSignalTypes={setDisabledSignalTypes}
        setDisabledChannels={setDisabledChannels}
      />

      <MiscSection
        open={openSections.misc}
        onToggle={() => toggleSection('misc')}
        disableAutoTp={disableAutoTp}
        setDisableAutoTp={setDisableAutoTp}
        volatilityGuard={volatilityGuard}
        setVolatilityGuard={setVolatilityGuard}
        touch={touch}
      />

      {/* SAVE */}
      <div style={{ display: 'flex', gap: 12, justifyContent: 'flex-end' }}>
        <button
          className="btn ghost"
          onClick={handleRestoreDefaults}
          disabled={saving}
          style={{ marginRight: 'auto', color: 'var(--neg)' }}
        >
          Restore defaults
        </button>
        <button className="btn ghost" onClick={handleDiscard}>
          Reset
        </button>
        <button className="btn primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving…' : 'Save configuration'}
        </button>
      </div>

      {error && <p style={{ color: 'var(--neg)', fontSize: 13 }}>{error}</p>}

      {dirty && !toast && (
        <div className="savebar">
          <span className="msg">
            <b>Unsaved changes</b>
          </span>
          <div className="acts">
            <button className="btn sm ghost" onClick={handleDiscard} disabled={saving}>
              Discard
            </button>
            <button className="btn sm primary" onClick={handleSave} disabled={saving}>
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </div>
      )}
      {toast && (
        <div className="toast">
          <Icon name="check" size={15} strokeWidth={2.6} /> Changes saved
        </div>
      )}
    </div>
  )
}
