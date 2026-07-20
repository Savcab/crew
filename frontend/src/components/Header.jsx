// Header — title, live agent meta, the pending-approval tray button, and the
// refresh-rate selector. The <select id="rate"> stays a native control (browser
// scripts and muscle memory poke it directly).
import Button from '@mui/material/Button'

export default function Header({ agents, pendingCount, onOpenPending, onRateChange,
  termWin, onTermWinToggle, workspaceKey }) {
  const running = agents.filter(a => a.runtime_alive).length
  const meta = agents.length
    ? `${agents.length} agent${agents.length === 1 ? '' : 's'} · ${running} running`
    : ''
  const project = !workspaceKey || workspaceKey === 'crew'
    ? 'default' : workspaceKey.replace(/^crew-/, '')
  return (
    <header>
      <h1>crew</h1>
      {project !== 'default' &&
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
