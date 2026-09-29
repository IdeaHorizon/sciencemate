import { cn } from "./cn";

type CardProps = {
  children: React.ReactNode;
  className?: string;
  /** Show an interactive hover/focus state */
  interactive?: boolean;
  /** Add an accent border on the left */
  accent?: "success" | "warning" | "danger" | "info" | "muted";
  onClick?: () => void;
};

export function Card({ children, className, interactive, accent, onClick }: CardProps) {
  return (
    <div
      className={cn(
        "ui-card",
        interactive && "ui-card-interactive",
        accent && `ui-card-accent-${accent}`,
        className,
      )}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {children}
    </div>
  );
}

export function CardHeader({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("ui-card-header", className)}>{children}</div>;
}

export function CardBody({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("ui-card-body", className)}>{children}</div>;
}

export function CardFooter({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("ui-card-footer", className)}>{children}</div>;
}

Card.Header = CardHeader;
Card.Body = CardBody;
Card.Footer = CardFooter;
