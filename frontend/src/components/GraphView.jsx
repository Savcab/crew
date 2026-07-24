// GraphView — the React-owned skeleton around the imperative graph canvas.
// graphEngine.js owns EVERYTHING inside #cgraph (and the text of #cgraph-meta,
// #zoomPct, #graphKeyboardStatus, plus the zoom buttons' onclick): this
// component renders those elements once and must never update their children,
// so React's reconciler and the engine can't fight. memo + no props achieves
// that — the parent re-rendering never touches this DOM.
import { memo } from 'react'
import Button from '@mui/material/Button'

export default memo(function GraphView({ onCreateAgent, onCreateWebhook }) {
  return (
    <div id="cgraph-wrap">
      <div id="cgraph-head">
        <span className="t">Crew graph</span>
        <span className="meta" id="cgraph-meta"></span>
        <span className="ghint" style={{ marginLeft: 'auto' }}>
          drag <b className="hdot">●</b> onto another node to connect · keyboard:
          focus node, C, target, Enter
        </span>
        <span className="zoomctl"
          title="scroll/pinch to zoom to your cursor (or Ctrl/Cmd + / −) · drag empty space or middle-drag to pan · Ctrl/Cmd 0 or Fit to frame all">
          <button className="btn sm" id="zoomOut" aria-label="zoom out">−</button>
          <span className="meta" id="zoomPct">100%</span>
          <button className="btn sm" id="zoomIn" aria-label="zoom in">+</button>
          <button className="btn sm" id="zoomFit" aria-label="zoom to fit">⊡ fit</button>
        </span>
        <Button id="addAgentBtn" variant="contained" disableElevation
          sx={{ ml: '12px' }} onClick={onCreateAgent}>+ Agent</Button>
        <Button id="addWebhookBtn" variant="outlined"
          onClick={onCreateWebhook}>+ Hook</Button>
      </div>
      <div className="sr-only" id="graphKeyboardStatus" role="status" aria-live="polite"></div>
      <div id="cgraph" aria-busy="true">
        <div className="empty" id="graphStatus" role="status" aria-live="polite">Loading crew…</div>
      </div>
    </div>
  )
}, () => true)
