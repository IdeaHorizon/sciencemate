/**
 * Tiny className combiner — same idea as `clsx`/`classnames` but no dep.
 * Filters out falsy values; joins the rest with spaces.
 */
export function cn(...args: Array<string | undefined | null | false>): string {
  return args.filter(Boolean).join(" ");
}
