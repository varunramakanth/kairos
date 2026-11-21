"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  deleteUser,
  inviteUser,
  listUsers,
  resendVerification,
  updateUser,
} from "@/client/sdk.gen";
import type { User } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { isAdmin, useMe } from "@/lib/me";
import { capLabel, parseCap } from "@/lib/users";

type Act = (
  run: () => Promise<{ error?: unknown }>,
  done?: string,
) => Promise<void>;

/** Admins invite people; each person's chats, tasks and memories stay their own. */
export default function UsersPage() {
  const me = useMe();
  const [users, setUsers] = useState<User[]>([]);
  const [email, setEmail] = useState("");
  const [cap, setCap] = useState("2");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    const { data, error: failure } = await listUsers();
    if (data) setUsers(data);
    else setError(errorMessage(failure));
  }, []);

  useEffect(() => {
    if (isAdmin(me)) void load();
  }, [load, me]);

  const act: Act = async (run, done) => {
    const { error: failure } = await run();
    if (failure) {
      setError(errorMessage(failure));
      setNotice(null);
    } else {
      setError(null);
      setNotice(done ?? null);
      await load();
    }
  };

  async function invite(event: FormEvent) {
    event.preventDefault();
    const address = email.trim();
    if (!address) return;
    await act(
      () =>
        inviteUser({ body: { email: address, daily_cap_usd: parseCap(cap) } }),
      `Invited ${address}. They get two emails: a temporary password from Amazon Cognito, and a link from Amazon SES that lets Kairos email them.`,
    );
    setEmail("");
  }

  if (!isAdmin(me)) {
    return (
      <p className="p-6 text-muted-foreground text-sm">
        Only the admin can manage users.
      </p>
    );
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 overflow-y-auto px-4 py-6 md:px-6">
      <header>
        <h1 className="font-semibold text-xl">Users</h1>
        <p className="text-muted-foreground text-sm">
          People you invite get their own chats, tasks, memories and approvals;
          nobody sees anyone else&apos;s. Everyone shares your model and search
          keys, so each person has a daily spend cap.
        </p>
      </header>
      {error && <p className="text-destructive text-sm">{error}</p>}
      {notice && <p className="text-sm">{notice}</p>}

      <form
        onSubmit={invite}
        className="flex flex-wrap items-end gap-2 rounded-lg border p-4"
      >
        <label className="flex min-w-56 flex-1 flex-col gap-1 text-sm">
          Email
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="friend@example.com"
            className="rounded-md border bg-background px-3 py-2"
          />
        </label>
        <label className="flex w-36 flex-col gap-1 text-sm">
          Daily cap (USD)
          <input
            inputMode="decimal"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
            placeholder="none"
            className="rounded-md border bg-background px-3 py-2"
          />
        </label>
        <Button type="submit">Invite</Button>
      </form>

      <ul className="divide-y rounded-lg border">
        {users.map((user) => (
          <UserRow
            key={user.id}
            user={user}
            self={user.id === me.id}
            onAct={act}
          />
        ))}
      </ul>
    </div>
  );
}

function UserRow({
  user,
  self,
  onAct,
}: {
  user: User;
  self: boolean;
  onAct: Act;
}) {
  const [cap, setCap] = useState(
    user.daily_cap_usd === null ? "" : String(user.daily_cap_usd),
  );
  const path = { user_id: user.id };
  const name = user.email ?? user.id;
  const member = user.role === "member";

  function saveCap() {
    const value = parseCap(cap);
    void onAct(
      () =>
        updateUser({
          path,
          body: value === null ? { clear_cap: true } : { daily_cap_usd: value },
        }),
      `Cap for ${name}: ${capLabel(value)}.`,
    );
  }

  function remove() {
    const sure = window.confirm(
      `Delete ${name}? Their sign-in, chats, tasks and memories are removed for good.`,
    );
    if (sure) void onAct(() => deleteUser({ path }), `Deleted ${name}.`);
  }

  return (
    <li className="space-y-2 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{name}</span>
        <span className="rounded bg-muted px-1.5 text-xs">{user.role}</span>
        {self && <span className="text-muted-foreground text-xs">you</span>}
        {user.disabled && (
          <span className="rounded bg-destructive px-1.5 text-white text-xs">
            disabled
          </span>
        )}
        <span className="ml-auto text-muted-foreground text-xs">
          {user.email_verified
            ? "email verified"
            : "waiting for email verification"}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">Daily cap:</span>
        <input
          aria-label={`Daily cap for ${name}`}
          inputMode="decimal"
          value={cap}
          onChange={(e) => setCap(e.target.value)}
          placeholder="none"
          className="w-24 rounded-md border bg-background px-2 py-1"
        />
        <Button size="sm" variant="outline" onClick={saveCap}>
          Save
        </Button>
        {!user.email_verified && user.email && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              void onAct(
                () => resendVerification({ path }),
                `Sent ${user.email} a new verification link.`,
              )
            }
          >
            Resend verification
          </Button>
        )}
        {member && !self && (
          <>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                void onAct(() =>
                  updateUser({ path, body: { disabled: !user.disabled } }),
                )
              }
            >
              {user.disabled ? "Enable" : "Disable"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive"
              onClick={remove}
            >
              Delete
            </Button>
          </>
        )}
      </div>
    </li>
  );
}
