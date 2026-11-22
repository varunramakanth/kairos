/** A cap typed in a box: blank means none; anything else must be a number ≥ 0. */
export function parseCap(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

export function capLabel(cap: number | null): string {
  return cap === null ? "no cap of their own" : `$${cap.toFixed(2)} a day`;
}
