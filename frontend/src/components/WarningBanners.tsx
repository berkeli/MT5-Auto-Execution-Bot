import { useEffect, useState, type ReactNode } from 'react'
import { Icon } from './Icon'
import type { Config, StatusData } from '../types'

const SYMBOL_FLOOR = 50

interface BannerDef {
  id: string
  title: string
  text: ReactNode
  tone: 'warn' | 'danger'
}

// Minutes the named timezone is ahead of UTC at `date` (handles DST).
function tzOffsetMinutes(tz: string, date: Date): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date)
  const m: Record<string, string> = {}
  for (const p of parts) m[p.type] = p.value
  const asUTC = Date.UTC(+m.year, +m.month - 1, +m.day, +m.hour, +m.minute, +m.second)
  return (asUTC - date.getTime()) / 60000
}

// Render an HH:MM wall-clock time in the given timezone as the viewer's local time.
function zonedTimeToLocalLabel(hhmm: string, tz: string): string {
  const [h, mm] = hhmm.split(':').map(Number)
  const now = new Date()
  const offMin = tzOffsetMinutes(tz, now)
  const d = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const m: Record<string, string> = {}
  for (const p of d) m[p.type] = p.value
  const utcMs = Date.UTC(+m.year, +m.month - 1, +m.day, h, mm) - offMin * 60000
  return new Date(utcMs).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function buildBanners(status: StatusData | null, config: Config | null): BannerDef[] {
  const banners: BannerDef[] = []

  // Two consecutive windows, one gate. The earlier daily_start..sl_strip_start slice
  // ("late-market") only blocks new orders — working limits and filled positions carry
  // on. The sl_strip_start..daily_end slice ("spread hour") is the teardown: pendings
  // cancelled, SLs stripped, TP engine crypto-only.
  if (status?.sl_strip_active) {
    const sh = config?.spread_hour
    const window =
      sh != null
        ? `${zonedTimeToLocalLabel(sh.sl_strip_start, sh.timezone)}–${zonedTimeToLocalLabel(sh.daily_end, sh.timezone)}`
        : null
    banners.push({
      id: 'spread-hour',
      tone: 'warn',
      title: window ? `Spread hour (${window})` : 'Spread hour',
      text: 'Pending orders are cancelled and stop-losses on filled positions are removed to avoid the spread spike. Crypto is exempt.',
    })
  } else if (status?.spread_hour_active) {
    const sh = config?.spread_hour
    const window =
      sh != null
        ? `${zonedTimeToLocalLabel(sh.daily_start, sh.timezone)}–${zonedTimeToLocalLabel(sh.sl_strip_start, sh.timezone)}`
        : null
    banners.push({
      id: 'late-market',
      tone: 'warn',
      title: window ? `Late-market window (${window})` : 'Late-market window',
      text: 'New orders are blocked to avoid late-market activations. Filled positions stay active until spread hour. Crypto is exempt.',
    })
  }

  if (status?.market_closed) {
    banners.push({
      id: 'market-closed',
      tone: 'warn',
      title: 'Market closed',
      text: 'No trades are placed while the market is closed, except for crypto.',
    })
  }

  if (status?.algo_trading_disabled) {
    banners.push({
      id: 'algo-disabled',
      tone: 'danger',
      title: 'Auto-trading is disabled in MT5',
      text: (
        <>
          Enable <span className="mono">Algo Trading</span> (also called{' '}
          <span className="mono">Auto Trading</span>) in your MT5 terminal — the button is usually
          at the top-right of the screen.
        </>
      ),
    })
  }

  const count = status?.symbol_count ?? 0
  if (status?.mt5_connected && count > 0 && count < SYMBOL_FLOOR) {
    banners.push({
      id: 'symbols-low',
      tone: 'warn',
      title: `Only ${count} symbols loaded`,
      text: (
        <>
          Some symbols may be hidden. In MT5 Market Watch, right-click and press{' '}
          <span className="mono">Show All</span>. If you aren&apos;t using ICMarkets, make sure all
          symbols are matched to your broker.
        </>
      ),
    })
  }

  const path = config?.mt5_terminal_path?.trim()
  if (path && !/terminal64\.exe$/i.test(path)) {
    banners.push({
      id: 'wrong-path',
      tone: 'danger',
      title: 'MT5 terminal path looks wrong',
      text: (
        <>
          Double-check the terminal path in Settings — it should end in{' '}
          <span className="mono">terminal64.exe</span>.
        </>
      ),
    })
  }

  return banners
}

interface Props {
  status: StatusData | null
  config: Config | null
}

export function WarningBanners({ status, config }: Props) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())

  const banners = buildBanners(status, config)
  const activeKey = banners
    .map(b => b.id)
    .sort()
    .join(',')

  // Re-arm a banner once its condition clears so it can show again if it recurs.
  useEffect(() => {
    const active = new Set(activeKey ? activeKey.split(',') : [])
    setDismissed(prev => {
      const next = new Set([...prev].filter(id => active.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [activeKey])

  const visible = banners.filter(b => !dismissed.has(b.id))
  if (visible.length === 0) return null

  return (
    <>
      {visible.map(b => (
        <div key={b.id} className={`license-banner warn-banner ${b.tone}`}>
          <Icon name="bell" size={18} />
          <div className="license-banner-body">
            <div className="license-banner-title">{b.title}</div>
            <div className="license-banner-text">{b.text}</div>
          </div>
          <button
            className="banner-close"
            aria-label="Dismiss"
            onClick={() => setDismissed(s => new Set(s).add(b.id))}
          >
            <Icon name="x" size={16} />
          </button>
        </div>
      ))}
    </>
  )
}
