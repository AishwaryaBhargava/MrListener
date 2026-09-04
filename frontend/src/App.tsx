import { Route, Routes } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import RecordPage from './pages/RecordPage'
import MeetingsPage from './pages/MeetingsPage'
import MeetingDetailPage from './pages/MeetingDetailPage'
import SettingsPage from './pages/SettingsPage'

export default function App() {
  return (
    <div className="shell">
      <Sidebar />
      <main className="main">
        <Routes>
          <Route path="/" element={<RecordPage />} />
          <Route path="/meetings" element={<MeetingsPage />} />
          <Route path="/meetings/:id" element={<MeetingDetailPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<RecordPage />} />
        </Routes>
      </main>
    </div>
  )
}
