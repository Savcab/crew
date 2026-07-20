// Dock — React-owned skeleton for the agent terminal dock. dockCore.js wires
// all behavior (buttons, resize, focus, the TerminalPane) against these ids in
// a mount effect; React must never update the children dockCore mutates
// (#dockName, #dockMeta, #dockDot, #dockStart visibility), so the skeleton is
// static and the component memoizes to a single mount-time render.
import { memo, useEffect } from 'react'
import { api } from '../api.js'
import { TerminalPane } from '../term.js'
import { createDock } from '../dockCore.js'

export default memo(function Dock({ getWorkers, onDockChange, onShowIdentity, toast, onController }) {
  useEffect(() => {
    onController(createDock({
      TerminalPane, api, getWorkers, onDockChange, onShowIdentity, toast,
    }))
    // dockCore has no teardown — it lives for the page (as it always has).
  }, [])
  return (
    <div id="dock">
      <div id="dockResize" title="drag to resize"></div>
      <div id="dock-head">
        <span className="dot" id="dockDot"></span>
        <span id="dockName">agent</span>
        <span className="meta" id="dockMeta"></span>
        <button className="btn sm" id="dockIdentity"
          title="show this agent's identity — role, home, who it talks to">ⓘ identity</button>
        <button className="btn sm primary" id="dockStart" style={{ display: 'none' }}
          title="start this agent's session or runtime">▶ start runtime</button>
        <button className="btn sm" id="dockPrev" style={{ marginLeft: 'auto' }}
          title="previous agent">‹</button>
        <button className="btn sm" id="dockNext" title="next agent">›</button>
        <button className="btn sm" id="dockMax" title="maximize / restore">⤢</button>
        <button className="btn sm" id="dockClose">✕ close</button>
      </div>
      <span id="dockLiveBadge">⌨ keys → this terminal · Ctrl+Esc to detach</span>
      <div id="dockPanes">
        <div id="dockTerm" className="dockpane"></div>
      </div>
    </div>
  )
}, () => true)
