/**
 * Light, dark or follow the system. The choice is kept in localStorage and applied
 * as the `dark` class on <html> (globals.css uses `.dark`). THEME_SCRIPT runs before
 * the first paint so a dark page never flashes light.
 */
export type Theme = "light" | "dark" | "system";

export const THEME_KEY = "kairos.theme";
export const THEME_CHANGED = "kairos:theme-changed";

export const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem("${THEME_KEY}")||"system";var d=t==="dark"||(t==="system"&&matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.classList.toggle("dark",d);}catch(e){}})();`;

export function storedTheme(): Theme {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function resolvesDark(theme: Theme, systemDark: boolean): boolean {
  return theme === "dark" || (theme === "system" && systemDark);
}

export function applyTheme(theme: Theme): void {
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle(
    "dark",
    resolvesDark(theme, systemDark),
  );
}

export function setTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Private mode: the choice lasts for this page only.
  }
  applyTheme(theme);
  window.dispatchEvent(new Event(THEME_CHANGED));
}

/** light → dark → system → light. */
export function nextTheme(theme: Theme): Theme {
  return theme === "light" ? "dark" : theme === "dark" ? "system" : "light";
}
