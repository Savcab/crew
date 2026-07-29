// Boot the crew dashboard. xtermGlobals must be first: term.js reads
// window.Terminal / window.FitAddon when a pane is constructed.
import './xtermGlobals.js'
import '@xterm/xterm/css/xterm.css'
import './app.css'
import { createRoot } from 'react-dom/client'
import { ThemeProvider } from '@mui/material/styles'
import { theme } from './theme.js'
import App from './App.jsx'
import TermWindow from './components/TermWindow.jsx'
import GraphsGallery from './components/GraphsGallery.jsx'
import SettingsPage from './components/SettingsPage.jsx'

// /?view=term is the second-window terminal viewer (dual-monitor flow);
// /?view=graphs is the apps gallery (every project is one graph);
// /?view=settings is the crew-wide settings page.
// Everything else is the normal dashboard.
const view = new URLSearchParams(window.location.search).get('view')

// No StrictMode: the graph canvas and the dock terminal are imperative islands
// wired once per mount; dev double-invoked effects would double their listeners.
createRoot(document.getElementById('root')).render(
  <ThemeProvider theme={theme}>
    {view === 'term' ? <TermWindow />
      : view === 'graphs' ? <GraphsGallery />
      : view === 'settings' ? <SettingsPage />
      : <App />}
  </ThemeProvider>,
)
