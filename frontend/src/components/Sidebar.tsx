import { NavLink, Link } from 'react-router-dom'
import { ListIcon, MicIcon, SettingsIcon, WaveformIcon } from './Icons'
import { useHealth } from '../hooks/useHealth'

const NAV = [
  { to: '/', label: 'Record', icon: MicIcon, end: true },
  { to: '/meetings', label: 'Meetings', icon: ListIcon, end: false },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, end: false },
]

export function Sidebar() {
  const health = useHealth()
  const groqReady = health?.groq_key_set === true

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">
          <WaveformIcon size={18} />
        </span>
        <span className="brand-name">MrListener</span>
      </div>

      <nav className="nav">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
          >
            <Icon size={17} />
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="sidebar-foot">
        {/* Clicking it goes straight to where the key is entered. */}
        <Link
          to="/settings"
          className={groqReady ? 'health-pill' : 'health-pill off'}
          title={groqReady ? 'Groq API key is set' : 'Add your Groq API key in Settings'}
        >
          <span className="health-dot" />
          {groqReady ? 'Groq connected' : 'Groq key missing'}
        </Link>
      </div>
    </aside>
  )
}
