import { cn } from "./cn";

/**
 * Score badge for review rubrics (1-5 scale).
 *
 * Color rules:
 *   5     — strong green
 *   4     — green
 *   3     — amber
 *   2     — orange
 *   1     — red
 *   null  — muted gray
 */
export function ScoreBadge({
  score,
  outOf = 5,
  size = "md",
  showOutOf = true,
  className,
}: {
  score: number | null | undefined;
  outOf?: number;
  size?: "sm" | "md" | "lg";
  showOutOf?: boolean;
  className?: string;
}) {
  let kindClass = "score-muted";
  if (typeof score === "number" && !Number.isNaN(score)) {
    if (score >= 4.5) kindClass = "score-excellent";
    else if (score >= 3.5) kindClass = "score-good";
    else if (score >= 2.5) kindClass = "score-fair";
    else if (score >= 1.5) kindClass = "score-weak";
    else kindClass = "score-bad";
  }
  const display = score === null || score === undefined ? "—" : score.toFixed(1);
  return (
    <span className={cn("ui-score", `ui-score-${size}`, kindClass, className)}>
      <span className="ui-score-value">{display}</span>
      {showOutOf && <span className="ui-score-of">/{outOf}</span>}
    </span>
  );
}
