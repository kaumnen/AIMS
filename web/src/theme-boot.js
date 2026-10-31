// Runs in <head> before first paint, so a stored theme never flashes the system one. Keep in
// step with applyTheme in lib/theme.ts.
try {
  const theme = localStorage.getItem("aims.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  // storage refused: CSS follows the system
}
