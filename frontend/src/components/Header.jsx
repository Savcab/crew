// Header — title, live agent meta, the pending-approval tray button, and the
// refresh-rate selector. The <select id="rate"> stays a native control (browser
// scripts and muscle memory poke it directly).
import Button from '@mui/material/Button'

export default function Header({ agents, webhooks, pendingCount, onOpenPending, onRateChange,
  termWin, onTermWinToggle, workspaceKey, projectTitle }) {
  const running = agents.filter(a => a.runtime_alive).length
  const hookCount = (webhooks || []).length
  const parts = []
  if (agents.length) {
    parts.push(`${agents.length} agent${agents.length === 1 ? '' : 's'}`)
    parts.push(`${running} running`)
  }
  if (hookCount) parts.push(`${hookCount} hook${hookCount === 1 ? '' : 's'}`)
  const meta = parts.join(' · ')
  const slug = !workspaceKey || workspaceKey === 'crew'
    ? 'default' : workspaceKey.replace(/^crew-/, '')
  const project = projectTitle || slug
  return (
    <header>
      <h1>crew</h1>
      {slug !== 'default' &&
        <span className="proj" id="projectName">/ {project}</span>}
      <Button id="graphsHomeBtn" size="small" title="all graphs"
        onClick={() => { window.location.href = '/?view=graphs' }}>⌂ graphs</Button>
      <span className="meta" id="meta">{meta}</span>
      {pendingCount > 0 &&
        <Button id="pendingBtn" size="small" color="warning"
          title="approval requests needing attention" onClick={onOpenPending}>
          ⏳ approvals <span className="badge" id="pendingBadge">{pendingCount}</span>
        </Button>}
      <Button id="termWinToggle" size="small" sx={{ ml: 'auto' }}
        variant={termWin ? 'contained' : 'outlined'} disableElevation
        aria-pressed={termWin ? 'true' : 'false'}
        title="open agent terminals in a second browser window (put it on another monitor)"
        onClick={onTermWinToggle}>⧉ 2nd window</Button>
      <Button id="settingsBtn" size="small" title="crew-wide settings"
        onClick={() => { window.location.href = '/?view=settings' }}>⚙ settings</Button>
      <label>refresh{' '}
        <select id="rate" defaultValue="1500"
          onChange={e => onRateChange(parseInt(e.target.value, 10) || 0)}>
          <option value="1500">1.5s</option>
          <option value="1000">1s</option>
          <option value="3000">3s</option>
          <option value="0">off</option>
        </select>
      </label>
    </header>
  )
}
