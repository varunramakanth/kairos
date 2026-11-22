import {
  Brain,
  CalendarClock,
  FlaskConical,
  Inbox,
  type LucideIcon,
  ScrollText,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";
import type { Role } from "@/client/types.gen";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Extra words the command palette matches on. */
  keywords?: string;
  /** Shown to admins only (ADR-018); the API refuses members anyway. */
  adminOnly?: boolean;
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
    adminOnly: true,
  },
  { href: "/audit", label: "Audit log", icon: ScrollText, keywords: "history" },
  {
    href: "/users",
    label: "Users",
    icon: Users,
    keywords: "invite people members share",
    adminOnly: true,
  },
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

/** The pages ``role`` can use. */
export function navFor(role: Role): readonly NavItem[] {
  return NAV_ITEMS.filter((item) => !item.adminOnly || role === "admin");
}

export function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}
