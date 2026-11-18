import {
  Brain,
  CalendarClock,
  FlaskConical,
  Inbox,
  type LucideIcon,
  ScrollText,
  Settings,
  Sparkles,
} from "lucide-react";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Extra words the command palette matches on. */
  keywords?: string;
};

/** The app's pages, shared by the sidebar and the command palette. */
export const NAV_ITEMS: readonly NavItem[] = [
  {
    href: "/inbox",
    label: "Approvals",
    icon: Inbox,
    keywords: "inbox pending",
  },
  {
    href: "/tasks",
    label: "Tasks & watches",
    icon: CalendarClock,
    keywords: "jobs schedule watch",
  },
  { href: "/memory", label: "Memory", icon: Brain, keywords: "remember facts" },
  {
    href: "/evals",
    label: "Evals",
    icon: FlaskConical,
    keywords: "scores judge review",
  },
  { href: "/audit", label: "Audit log", icon: ScrollText, keywords: "history" },
  {
    href: "/settings",
    label: "Settings",
    icon: Settings,
    keywords: "kill switch push email spend",
  },
  {
    href: "/welcome",
    label: "Welcome & setup",
    icon: Sparkles,
    keywords: "onboarding checklist tour help",
  },
];

export function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}
