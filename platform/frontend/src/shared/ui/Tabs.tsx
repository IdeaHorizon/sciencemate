import { cn } from "./cn";

type Tab<T extends string> = {
  id: T;
  label: string;
  icon?: React.ReactNode;
  badge?: React.ReactNode;
};

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
  className,
}: {
  tabs: Tab<T>[];
  active: T;
  onChange: (id: T) => void;
  className?: string;
}) {
  return (
    <div className={cn("ui-tabs", className)} role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          aria-selected={active === tab.id}
          className={cn("ui-tab", active === tab.id && "ui-tab-active")}
          onClick={() => onChange(tab.id)}
        >
          {tab.icon && <span className="ui-tab-icon">{tab.icon}</span>}
          <span className="ui-tab-label">{tab.label}</span>
          {tab.badge && <span className="ui-tab-badge">{tab.badge}</span>}
        </button>
      ))}
    </div>
  );
}
