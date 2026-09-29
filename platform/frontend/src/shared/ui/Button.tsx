import { forwardRef } from "react";
import { cn } from "./cn";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "success";
type ButtonSize = "sm" | "md" | "lg";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  iconLeft?: React.ReactNode;
  iconRight?: React.ReactNode;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    { variant = "secondary", size = "md", loading, iconLeft, iconRight, children, className, disabled, ...rest },
    ref,
  ) {
    return (
      <button
        ref={ref}
        className={cn(
          "ui-button",
          `ui-button-${variant}`,
          `ui-button-${size}`,
          loading && "ui-button-loading",
          className,
        )}
        disabled={disabled || loading}
        {...rest}
      >
        {iconLeft && <span className="ui-button-icon">{iconLeft}</span>}
        <span className="ui-button-label">{children}</span>
        {iconRight && <span className="ui-button-icon">{iconRight}</span>}
      </button>
    );
  },
);
