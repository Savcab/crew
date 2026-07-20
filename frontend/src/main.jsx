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

// /?view=term is the second-window terminal viewer (dual-monitor flow): no
// graph, just a full-window dock driven by BroadcastChannel from the graph
// window. Everything else is the normal dashboard.
const isTermWindow =
  new URLSearchParams(window.location.search).get('view') === 'term'

// No StrictMode: the graph canvas and the dock terminal are imperative islands
// wired once per mount; dev double-invoked effects would double their listeners.
createRoot(document.getElementById('root')).render(
  <ThemeProvider theme={theme}>
    {isTermWindow ? <TermWindow /> : <App />}
  </ThemeProvider>,
)
