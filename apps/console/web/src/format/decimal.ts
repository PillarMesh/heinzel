/**
 * A warehouse decimal as a person reads it.
 *
 * The engine returns its declared scale in full, so a currency amount arrives as
 * `30.000000000`. Trailing zeros past the second place are noise the reader did not ask for;
 * everything else is left exactly as the warehouse wrote it, because this is a governed value
 * and rounding it here would make the console the author of a number it only carries.
 */
export function displayDecimal(value: string): string {
  if (!/^-?\d+\.\d+$/.test(value)) {
    return value
  }
  const [integer, fraction] = value.split(".") as [string, string]
  let visibleFraction = fraction
  while (visibleFraction.length > 2 && visibleFraction.endsWith("0")) {
    visibleFraction = visibleFraction.slice(0, -1)
  }
  return `${integer}.${visibleFraction}`
}
