import { cn } from "./cn";

type BadgeKind =
  | "neutral"
  | "accent"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "muted";

const KIND_CLASS: Record<BadgeKind, string> = {
  neutral: "badge-neutral",
  accent: "badge-accent",
  success: "badge-success",
  warning: "badge-warning",
  danger: "badge-danger",
  info: "badge-info",
  muted: "badge-muted",
};

export function Badge({
  children,
  kind = "neutral",
  size = "sm",
  className,
}: {
  children: React.ReactNode;
  kind?: BadgeKind;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <span
      className={cn("ui-badge", KIND_CLASS[kind], size === "md" && "ui-badge-md", className)}
    >
      {children}
    </span>
  );
}
