"use client";

import { createContext, type ReactNode, useContext } from "react";
import type { Me } from "@/client/types.gen";

/** Before `/me` answers, nobody is treated as an admin. */
const NOBODY: Me = { id: "", email: null, role: "member", auth_enabled: false };

const MeContext = createContext<Me>(NOBODY);

export function MeProvider({ me, children }: { me: Me; children: ReactNode }) {
  return <MeContext value={me}>{children}</MeContext>;
}

/** The signed-in user (ADR-018). The API enforces roles; this only decides what to show. */
export function useMe(): Me {
  return useContext(MeContext);
}

export function isAdmin(me: Me): boolean {
  return me.role === "admin";
}
