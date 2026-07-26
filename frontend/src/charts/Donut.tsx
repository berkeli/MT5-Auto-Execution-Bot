import { useState, useEffect } from 'react'

export interface DonutSegment {
  value: number
  color: string
}

interface DonutProps {
  segments: DonutSegment[]
  label: string
  caption: string
  size?: number
  strokeW?: number
}

export function Donut({ segments, label, caption, size = 160, strokeW = 13 }: DonutProps) {
  const r = size / 2 - strokeW / 2 - 2
  const c = size / 2
  const circ = 2 * Math.PI * r
  const [draw, setDraw] = useState(false)

  useEffect(() => {
    const t = setTimeout(() => setDraw(true), 60)
    return () => clearTimeout(t)
  }, [])

  const total = segments.reduce((s, seg) => s + seg.value, 0)
  const drawn = segments.filter(seg => seg.value > 0)

  let offset = 0
  const arcs = drawn.map(seg => {
    const len = total > 0 ? (seg.value / total) * circ : 0
    const arc = { color: seg.color, len, offset }
    offset += len
    return arc
  })

  return (
    <svg viewBox={`0 0 ${size} ${size}`} style={{ width: size, height: size }}>
      <circle cx={c} cy={c} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={strokeW} />
      {arcs.map((a, i) => (
        <circle
          key={i}
          cx={c}
          cy={c}
          r={r}
          fill="none"
          stroke={a.color}
          strokeWidth={strokeW}
          strokeLinecap={arcs.length > 1 ? 'butt' : 'round'}
          strokeDasharray={`${draw ? a.len : 0} ${circ}`}
          strokeDashoffset={-a.offset}
          transform={`rotate(-90 ${c} ${c})`}
          style={{ transition: 'stroke-dasharray 1s cubic-bezier(.4,0,.2,1)' }}
        />
      ))}
      <text
        x={c}
        y={c - 2}
        textAnchor="middle"
        fontSize="30"
        fontWeight="700"
        fill="var(--text)"
        fontFamily="var(--font)"
      >
        {label}
      </text>
      <text
        x={c}
        y={c + 18}
        textAnchor="middle"
        fontSize="11"
        fill="var(--text-3)"
        fontFamily="var(--font)"
        letterSpacing="0.06em"
      >
        {caption}
      </text>
    </svg>
  )
}
