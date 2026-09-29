import { SettingsShell } from "@/features/settings/components/SettingsShell";

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return <SettingsShell>{children}</SettingsShell>;
}
