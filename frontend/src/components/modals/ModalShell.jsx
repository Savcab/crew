// ModalShell — MUI Dialog wearing the old modal's ids (#cmodal / #modalTitle /
// #modalBody / #modalClose) so browser scripts keep working. MUI supplies the
// focus trap, Escape handling, backdrop dismissal, and focus restore that
// modal.js hand-rolled.
import Dialog from '@mui/material/Dialog'
import DialogTitle from '@mui/material/DialogTitle'
import DialogContent from '@mui/material/DialogContent'
import IconButton from '@mui/material/IconButton'

export default function ModalShell({ title, onClose, children, maxWidth = 'sm' }) {
  return (
    <Dialog open onClose={onClose} maxWidth={maxWidth} fullWidth
      slotProps={{ root: { id: 'cmodal', className: 'show' } }}
      aria-labelledby="modalTitle">
      {/* Explicit id stops MUI auto-stamping the dialog's aria-labelledby id
          onto this <h2> — the legacy-compat span below owns #modalTitle. */}
      <DialogTitle id="modalTitleBar" sx={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        py: 1.25, fontSize: 14, fontWeight: 'bold',
      }}>
        <span id="modalTitle">{title}</span>
        <IconButton id="modalClose" aria-label="Close dialog" size="small"
          onClick={onClose} sx={{ fontSize: 18, lineHeight: 1 }}>×</IconButton>
      </DialogTitle>
      <DialogContent id="modalBody" dividers sx={{ fontSize: 12.5 }}>
        {children}
      </DialogContent>
    </Dialog>
  )
}
