// term.js reads window.Terminal / window.FitAddon at construction time (a seam
// the vitest contract suite uses to install fakes). In the built app the real
// implementations come from npm; publish them on window before any pane exists.
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'

window.Terminal = Terminal
window.FitAddon = { FitAddon }
