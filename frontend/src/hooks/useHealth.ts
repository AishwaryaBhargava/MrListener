import { useEffect, useState } from 'react'
import { api, type Health } from '../lib/api'

/** Polls /api/health so the sidebar pill reflects backend + key availability. */
export function useHealth(pollMs = 30000): Health | null {
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    let cancelled = false

    const load = async () => {
      try {
        const result = await api.health()
        if (!cancelled) setHealth(result)
      } catch {
        if (!cancelled) setHealth(null)
      }
    }

    void load()
    const timer = window.setInterval(load, pollMs)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [pollMs])

  return health
}
