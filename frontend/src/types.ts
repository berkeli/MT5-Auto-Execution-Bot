export interface StatusData {
  engine_running: boolean
  trading_active: boolean
  license_valid: boolean
  license_status?: string
  license_message?: string
  mt5_connected: boolean
  mt5_error?: string | null
  supabase_connected: boolean
  supabase_error?: string | null
  pending_count: number
  open_count: number
  trailing_count: number
  bot_version?: string
  shutdown_reason?: string | null
  shutting_down?: boolean
  update_available?: boolean
  update_version?: string | null
  update_notes?: string
  update_in_progress?: boolean
  update_progress?: number
  update_error?: string | null
  spread_hour_active?: boolean
  sl_strip_active?: boolean
  market_closed?: boolean
  algo_trading_disabled?: boolean
  symbol_count?: number
}

export interface LogEntry {
  level: string
  message: string
  timestamp: string
}

export interface LotExceptionConfig {
  symbol: string
  channel?: string
  signal_type: string
  mode: 'risk_percent' | 'fixed' | 'total_lot'
  value: number
}

export interface ExcludedTradeConfig {
  symbol: string
  signal_type: string
}

export interface ExcludedChannelAssetConfig {
  channel: string
  asset_class: string
}

export interface LotSizingConfig {
  mode: string
  risk_percent: number | Record<string, number>
  fixed_lot: number | Record<string, number>
  total_lot: number | Record<string, number>
  max_lot_per_order: number
  skip_limits_at?: number
  exceptions?: LotExceptionConfig[]
}

export interface AssetTPConfig {
  profit_threshold: number
  threshold_unit: string
  trailing_distance: number
  partial_close_percent?: number
}

export interface ScalpOverrideConfig {
  profit_threshold: number
  trailing_distance: number
  partial_close_percent?: number | null
}

export interface OneToOneConfig {
  profit_threshold: number
  overrides: Record<string, number>
}

export interface RiskyConfig {
  profit_threshold: number
  threshold_unit: string
  trailing_distance: number
  partial_close_percent: number
  stop_loss: number | null
  disabled_windows: string[]
  overrides: Record<string, ScalpOverrideConfig>
}

export interface TPConfig {
  follow_server_tp?: boolean
  partial_close_percent: number
  forex: AssetTPConfig
  forex_jpy: AssetTPConfig
  metals: AssetTPConfig
  indices: AssetTPConfig
  stocks: AssetTPConfig
  crypto: AssetTPConfig
  oil: AssetTPConfig
  scalp_overrides: Record<string, ScalpOverrideConfig>
  toll_overrides: Record<string, ScalpOverrideConfig>
  swing_overrides: Record<string, ScalpOverrideConfig>
  pa_overrides: Record<string, ScalpOverrideConfig>
  one_to_one: OneToOneConfig
  risky: RiskyConfig
  instrument_overrides: Record<string, Record<string, unknown>>
}

export interface SymbolSuffixRule {
  suffix: string
  asset_classes: string[]
}

export type SignalType = 'standard' | 'scalp' | 'swing' | 'toll' | 'pa' | '1-1' | 'risky'

export interface PollingConfig {
  supabase_interval_seconds: number
  tp_active_interval_seconds: number
  license_heartbeat_seconds: number
}

export interface SpreadHourConfig {
  daily_start: string
  stock_daily_start: string
  daily_end: string
  sl_strip_start: string
  sl_strip_stock_start: string
  timezone: string
  weekend_start_day: string
  weekend_end_day: string
}

export interface Config {
  license_key: string
  mt5_terminal_path: string
  lot_sizing: LotSizingConfig
  polling: PollingConfig
  symbol_map: Record<string, string>
  stock_suffix: string
  symbol_suffixes: SymbolSuffixRule[]
  stock_no_suffix: string[]
  excluded_symbols: string[]
  excluded_trades: ExcludedTradeConfig[]
  excluded_channel_assets: ExcludedChannelAssetConfig[]
  disabled_signal_types: string[]
  disabled_channels: string[]
  offset_instruments: string[]
  feed_max_staleness_seconds: number
  config_migrations?: string[]
  disclaimer_accepted?: boolean
  disable_auto_tp?: boolean
  volatility_guard?: boolean
  spread_hour: SpreadHourConfig
  tp_config: TPConfig
}

export interface AccountData {
  login: number
  balance: number
  equity: number
  margin: number
  margin_free: number
  leverage: number
  currency: string
  server?: string
  company?: string
  hedging?: boolean
}

export interface PositionData {
  ticket: number
  symbol: string
  direction: string
  volume: number
  price_open: number
  current_price: number
  sl: number
  profit: number
  is_trailing: boolean
  signal_id: number
  channel_id: string | null
  signal_type: SignalType
}

export interface PendingOrderData {
  ticket: number
  symbol: string
  direction: string
  volume: number
  price_level: number
  current_price: number
  sl: number
  distance: number
  signal_id: number
  channel_id: string | null
  signal_type: SignalType
}

export interface DashboardSummary {
  total_profit: number
  open_count: number
  pending_count: number
  trailing_count: number
}

export interface NearbySignalData {
  signal_id: number
  symbol: string
  mt5_symbol: string
  direction: string
  channel_id: string | null
  signal_type: SignalType
  limit_count: number
  closest_price: number
  closest_price_display: string
  current_price: number
  distance: number
  distance_display: string
  proximity_pct: number
  placed: boolean
  action: 'skip' | 'manual' | null
}

export type SignalAction = 'skip' | 'manual' | 'none'

export interface DashboardData {
  account: AccountData | null
  positions: PositionData[]
  pending_orders: PendingOrderData[]
  nearby_signals: NearbySignalData[]
  summary: DashboardSummary
  updated_at: string
}

export interface TradeData {
  signal_id: number
  symbol: string
  direction: string
  total_lots: number
  placed_at: string
  filled_at: string
  closed_at: string
  status: string
  breakeven: boolean
  signal_type: SignalType
  total_pnl: number
  fills_count: number
  cancelled_count: number
  channel_id: string | null
}

export interface HistoryStats {
  total_trades: number
  wins: number
  losses: number
  breakevens: number
  win_rate: number
  total_pnl: number
}

export interface HistoryData {
  trades: TradeData[]
  stats: HistoryStats
}

export type Page = 'dashboard' | 'history' | 'settings'
