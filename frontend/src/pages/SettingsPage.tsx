import { useEffect, useState } from 'react'
import { useToast } from '../components/Toast'
import { useHealth } from '../hooks/useHealth'
import { api, type Keys, type Settings } from '../lib/api'

/** The presets the language select offers; anything else is typed in. */
const LANGUAGES = [
  { value: '', label: 'Auto-detect' },
  { value: 'en', label: 'English (en)' },
  { value: 'hi', label: 'Hindi (hi)' },
  { value: 'es', label: 'Spanish (es)' },
  { value: 'fr', label: 'French (fr)' },
  { value: 'de', label: 'German (de)' },
]
const CUSTOM = '__custom__'

export default function SettingsPage() {
  const health = useHealth()
  const toast = useToast()
  const [settings, setSettings] = useState<Settings | null>(null)
  const [keys, setKeys] = useState<Keys | null>(null)
  const [groqDraft, setGroqDraft] = useState('')
  const [hfDraft, setHfDraft] = useState('')
  const [savingKeys, setSavingKeys] = useState(false)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [loadedSettings, loadedKeys] = await Promise.all([api.getSettings(), api.getKeys()])
        if (cancelled) return
        setSettings(loadedSettings)
        setKeys(loadedKeys)
      } catch {
        if (!cancelled) toast.error('Could not load your settings')
      }
    })()
    return () => {
      cancelled = true
    }
    // The toast api is stable; re-running this on it would refetch forever.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** Optimistic: the control moves, then the write is confirmed or rolled back. */
  const save = async (patch: Partial<Settings>) => {
    if (!settings) return
    const previous = settings
    setSettings({ ...settings, ...patch })
    try {
      setSettings(await api.putSettings(patch))
      toast.show('Saved')
    } catch (err) {
      setSettings(previous)
      toast.error(err instanceof Error ? err.message : 'Could not save that setting')
    }
  }

  const saveKeys = async () => {
    const groq = groqDraft.trim()
    const hf = hfDraft.trim()
    if (!groq && !hf) {
      toast.error('Paste a key first')
      return
    }
    setSavingKeys(true)
    try {
      const updated = await api.putKeys({
        ...(groq ? { groq_api_key: groq } : {}),
        ...(hf ? { hf_token: hf } : {}),
      })
      setKeys(updated)
      setGroqDraft('')
      setHfDraft('')
      toast.show('Keys saved and applied')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not save those keys')
    } finally {
      setSavingKeys(false)
    }
  }

  const custom =
    settings != null &&
    settings.language_hint !== '' &&
    !LANGUAGES.some((entry) => entry.value === settings.language_hint)

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <p className="eyebrow">Configuration</p>
          <h1 className="page-title">Settings</h1>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <h2 className="card-title">API keys</h2>
        </div>
        <div className="card-body settings-body">
          <KeyRow
            label="Groq API key"
            hint="Whisper transcription and the notes model."
            status={keys?.groq_api_key}
            value={groqDraft}
            onChange={setGroqDraft}
          />
          <KeyRow
            label="Hugging Face token"
            hint="Downloads the pyannote diarization models. Accept both model licences first."
            status={keys?.hf_token}
            value={hfDraft}
            onChange={setHfDraft}
          />
          <div className="settings-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void saveKeys()}
              disabled={savingKeys}
            >
              {savingKeys ? 'Saving...' : 'Save keys'}
            </button>
            <span className="settings-note">
              Written to the .env file at the project root and applied immediately - no restart.
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2 className="card-title">Transcription</h2>
        </div>
        <div className="card-body settings-body">
          <SettingRow
            label="Language hint"
            hint="Whisper detects the language on its own; a hint helps on short or noisy audio."
          >
            <select
              className="select"
              value={custom ? CUSTOM : (settings?.language_hint ?? '')}
              onChange={(event) => {
                const next = event.target.value
                void save({ language_hint: next === CUSTOM ? 'en' : next })
              }}
              disabled={!settings}
            >
              {LANGUAGES.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
              <option value={CUSTOM}>Other ISO code...</option>
            </select>
            {custom && (
              <input
                className="input settings-inline-input"
                value={settings?.language_hint ?? ''}
                maxLength={12}
                onChange={(event) =>
                  setSettings(settings ? { ...settings, language_hint: event.target.value } : null)
                }
                onBlur={(event) => void save({ language_hint: event.target.value })}
                aria-label="ISO language code"
                spellCheck={false}
              />
            )}
          </SettingRow>

          <SettingRow
            label="Live window"
            hint="How much audio each live transcription window covers while recording."
          >
            <div className="settings-slider">
              <input
                type="range"
                min={10}
                max={60}
                step={5}
                value={settings?.live_window_seconds ?? 20}
                onChange={(event) =>
                  setSettings(
                    settings
                      ? { ...settings, live_window_seconds: Number(event.target.value) }
                      : null,
                  )
                }
                onMouseUp={(event) =>
                  void save({ live_window_seconds: Number(event.currentTarget.value) })
                }
                onKeyUp={(event) =>
                  void save({ live_window_seconds: Number(event.currentTarget.value) })
                }
                disabled={!settings}
                aria-label="Live window seconds"
              />
              <span className="settings-value">{settings?.live_window_seconds ?? 20}s</span>
            </div>
          </SettingRow>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2 className="card-title">Speakers</h2>
        </div>
        <div className="card-body settings-body">
          <SettingRow
            label="Identify speakers"
            hint="Runs locally at about real time on this laptop. Notes are written before it starts, so turning it off only costs the speaker labels."
          >
            <button
              type="button"
              className={settings?.diarization_enabled ? 'toggle on' : 'toggle'}
              role="switch"
              aria-checked={settings?.diarization_enabled ?? false}
              onClick={() => void save({ diarization_enabled: !settings?.diarization_enabled })}
              disabled={!settings}
              aria-label="Identify speakers"
            >
              <span className="toggle-knob" />
            </button>
          </SettingRow>

          <SettingRow
            label="Maximum speakers"
            hint="Leave empty to let the model decide. Set it when you know how many people were in the room."
          >
            <input
              className="input settings-number"
              type="number"
              min={2}
              max={20}
              value={settings?.max_speakers ?? ''}
              placeholder="Auto"
              onChange={(event) =>
                setSettings(
                  settings
                    ? {
                        ...settings,
                        max_speakers: event.target.value ? Number(event.target.value) : null,
                      }
                    : null,
                )
              }
              onBlur={(event) =>
                void save({ max_speakers: event.target.value ? Number(event.target.value) : null })
              }
              disabled={!settings || !settings.diarization_enabled}
              aria-label="Maximum speakers"
            />
          </SettingRow>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2 className="card-title">About</h2>
        </div>
        <div className="card-body settings-body">
          <AboutRow label="Version" value={health?.version ?? '--'} />
          <AboutRow label="Data folder" value={health?.data_dir ?? '--'} mono />
          <AboutRow
            label="ffmpeg on PATH"
            value={health ? (health.ffmpeg ? 'Yes' : 'No - recording cannot be converted') : '--'}
          />
        </div>
      </section>
    </div>
  )
}

function SettingRow({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="setting-row">
      <div className="setting-label">
        <span className="setting-name">{label}</span>
        {hint && <span className="setting-hint">{hint}</span>}
      </div>
      <div className="setting-control">{children}</div>
    </div>
  )
}

function KeyRow({
  label,
  hint,
  status,
  value,
  onChange,
}: {
  label: string
  hint: string
  status?: { set: boolean; last4: string | null }
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="setting-row">
      <div className="setting-label">
        <span className="setting-name">
          {label}
          <span className={status?.set ? 'key-state set' : 'key-state'}>
            {status?.set ? `Set ****${status.last4 ?? ''}` : 'Not set'}
          </span>
        </span>
        <span className="setting-hint">{hint}</span>
      </div>
      <div className="setting-control">
        <input
          className="input"
          type="password"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={status?.set ? 'Paste a new key to replace it' : 'Paste your key'}
          autoComplete="off"
          spellCheck={false}
          aria-label={label}
        />
      </div>
    </div>
  )
}

function AboutRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="setting-row">
      <div className="setting-label">
        <span className="setting-name">{label}</span>
      </div>
      <div className="setting-control">
        <span className={mono ? 'about-value mono' : 'about-value'}>{value}</span>
      </div>
    </div>
  )
}
