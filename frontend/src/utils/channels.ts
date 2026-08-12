const CHANNEL_NAMES: Record<string, string> = {
  '1512881044938817576': 'Daily Setup',
  '1512881096650391582': 'Scalps',
  '1512881161372962977': 'Forex Exotics',
  '1512880961992396971': 'Gold Tolls',
  '1512881450414768210': 'Indices',
  '1512882347052109855': 'Proper Calls',
  '1514629669536665730': 'LC22 Calls',
  '1512882497560645683': 'Legends',
  '1512881837800947863': 'Oil',
  '1512882277062021240': 'Crypto',
  '1512881542702039231': 'Stocks',
  '1512881710629523456': 'Swings',
  '1512881213017296906': 'Gold Swings',
  '1512881345758757005': 'Gold PA',
  '1512882862494580797': 'Gold 1-1',
  '1522144546299838524': 'Risky Gold',
  '1536971699201773608': 'Semi-Swing PA',
  '1512882437267390586': 'OT Calls',
  '1512882024145354833': 'General Tolls',
}

export function getChannelName(channelId: string | null | undefined): string {
  if (!channelId) return 'Unknown'
  return CHANNEL_NAMES[channelId] ?? 'Unknown'
}

export const CHANNELS: { id: string; name: string }[] = Object.entries(CHANNEL_NAMES).map(
  ([id, name]) => ({ id, name })
)
