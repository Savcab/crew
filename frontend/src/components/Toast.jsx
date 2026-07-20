// Toast — same #toast div and classes the old dashboard used (browser scripts
// and app.css key off them); React just drives the show/err classes.
import { useEffect, useState } from 'react'

export default function Toast({ msg }) {
  const [shown, setShown] = useState(false)
  useEffect(() => {
    if (!msg) return
    setShown(true)
    const t = setTimeout(() => setShown(false), 3500)
    return () => clearTimeout(t)
  }, [msg])
  return (
    <div className={'toast' + (shown ? ' show' : '') + (msg && msg.err ? ' err' : '')}
      id="toast" role={msg && msg.err ? 'alert' : 'status'} aria-live="polite"
      aria-atomic="true">{msg ? msg.text : ''}</div>
  )
}
