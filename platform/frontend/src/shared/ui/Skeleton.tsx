import { cn } from "./cn";

export function Skeleton({
  width,
  height,
  rounded = "md",
  className,
}: {
  width?: number | string;
  height?: number | string;
  rounded?: "sm" | "md" | "lg" | "pill";
  className?: string;
}) {
  return (
    <div
      className={cn("ui-skeleton", `ui-skeleton-r-${rounded}`, className)}
      style={{
        width: typeof width === "number" ? `${width}px` : width,
        height: typeof height === "number" ? `${height}px` : height,
      }}
    />
  );
}

export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn("ui-skeleton-text", className)}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          height={12}
          width={`${75 + (i === lines - 1 ? -20 : Math.random() * 25)}%`}
        />
      ))}
    </div>
  );
}
