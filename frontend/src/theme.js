// MUI theme tuned to the existing glass palette in app.css (:root vars), so
// MUI chrome and the ported graph/dock CSS read as one surface.
import { createTheme } from '@mui/material/styles'

export const theme = createTheme({
  palette: {
    mode: 'dark',
    background: { default: '#0d1117', paper: '#0a0d12' },
    primary: { main: '#58a6ff' },
    success: { main: '#3fb950' },
    warning: { main: '#d29922' },
    error: { main: '#f85149' },
    text: { primary: '#c9d1d9', secondary: '#8b949e' },
    divider: '#21262d',
  },
  typography: {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    fontSize: 12,
  },
  components: {
    MuiButton: {
      defaultProps: { size: 'small', variant: 'outlined' },
      styleOverrides: { root: { textTransform: 'none' } },
    },
    MuiTextField: {
      defaultProps: { size: 'small', fullWidth: true, variant: 'outlined' },
    },
    MuiDialog: {
      styleOverrides: {
        paper: {
          backgroundColor: '#0a0d12',
          border: '1px solid #21262d',
          backgroundImage: 'none',
        },
      },
    },
  },
})
