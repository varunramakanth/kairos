"use client";

import { BellRing, Mail, OctagonX, Power } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getSettings,
  listNotifications,
  sendTestNotification,
  setKillSwitch,
} from "@/client/sdk.gen";
import type { AppSettings, StoredNotification } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { formatWhen } from "@/lib/jobs";
import {
  currentSubscription,
  disablePush,
  enablePush,
  pushSupported,
} from "@/lib/push";

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [notifications, setNotifications] = useState<StoredNotification[]>([]);
  const [subscribed, setSubscribed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [s, n] = await Promise.all([getSettings(), listNotifications()]);
    if (s.data) setSettings(s.data);
    else setMessage(errorMessage(s.error));
    if (n.data) setNotifications(n.data);
    setSubscribed((await currentSubscription()) !== null);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggleActions() {
    if (!settings) return;
    const turningOff = settings.actions_enabled;
    if (
      turningOff &&
      !window.confirm("Stop all agent actions and pause every scheduled job?")
    )
      return;
    const { data } = await setKillSwitch({
      body: { actions_enabled: !settings.actions_enabled },
    });
    if (data) setSettings(data);
  }

  async function togglePush() {
    if (!settings?.vapid_public_key) return;
    if (subscribed) {
      await disablePush();
      setMessage("Push is off for this browser.");
    } else {
      const failure = await enablePush(settings.vapid_public_key);
      setMessage(failure ?? "Push is on for this browser.");
    }
    await load();
  }

  async function test(channels: ("console" | "email" | "push")[]) {
    await sendTestNotification({ body: { channels } });
    setMessage(`Test queued for ${channels.join(", ")}.`);
    window.setTimeout(load, 3000);
  }

  if (!settings)
    return <p className="p-6 text-muted-foreground">{message ?? "Loading…"}</p>;

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 overflow-y-auto px-6 py-6">
      <h1 className="font-semibold text-xl">Settings</h1>
      {message && (
        <p className="rounded-md bg-muted px-3 py-2 text-sm">{message}</p>
      )}

      <section
        className={`space-y-2 rounded-lg border p-4 ${settings.actions_enabled ? "" : "border-destructive bg-destructive/5"}`}
      >
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 font-medium">
              <OctagonX className="size-4" />
              Kill switch
            </h2>
            <p className="text-muted-foreground text-sm">
              {settings.actions_enabled
                ? "Agent actions are allowed, each one after your approval."
                : "Agent actions are OFF. Every side effect is refused and scheduled jobs are paused."}
            </p>
          </div>
          <Button
            variant={settings.actions_enabled ? "destructive" : "default"}
            onClick={toggleActions}
          >
            <Power />
            {settings.actions_enabled
              ? "Stop everything"
              : "Allow actions again"}
          </Button>
        </div>
        {!settings.actions_enabled && (
          <p className="text-muted-foreground text-xs">
            Paused jobs stay paused: resume the ones you want on the Tasks page.
          </p>
        )}
      </section>

      <section className="space-y-2 rounded-lg border p-4">
        <h2 className="flex items-center gap-2 font-medium">
          <Mail className="size-4" />
          Email
        </h2>
        {settings.email_configured ? (
          <>
            <p className="text-sm">
              Alerts, approvals and the weekly digest (Sundays 18:00) go to{" "}
              <strong>{settings.email_to}</strong>.
            </p>
            <Button variant="outline" size="sm" onClick={() => test(["email"])}>
              Send a test email
            </Button>
          </>
        ) : (
          <p className="text-muted-foreground text-sm">
            Not set up. Add <code>KAIROS_EMAIL_TO</code>,{" "}
            <code>KAIROS_SMTP_USERNAME</code> and{" "}
            <code>KAIROS_SMTP_PASSWORD</code> (a Gmail app password) to{" "}
            <code>.env</code> and restart the API.
          </p>
        )}
      </section>

      <section className="space-y-2 rounded-lg border p-4">
        <h2 className="flex items-center gap-2 font-medium">
          <BellRing className="size-4" />
          Browser push
        </h2>
        {!settings.push_configured ? (
          <p className="text-muted-foreground text-sm">
            Not set up. Run <code>uv run kairos vapid-keys</code>, paste the two
            lines into <code>.env</code> and restart the API.
          </p>
        ) : !pushSupported() ? (
          <p className="text-muted-foreground text-sm">
            This browser does not support push. On iPhone, add Kairos to the
            Home Screen first.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={togglePush}>
              {subscribed
                ? "Turn off for this browser"
                : "Turn on for this browser"}
            </Button>
            {subscribed && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => test(["push"])}
              >
                Send a test push
              </Button>
            )}
          </div>
        )}
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Recent notifications</h2>
          <Button variant="ghost" size="sm" onClick={() => test(["console"])}>
            Test (log only)
          </Button>
        </div>
        {notifications.length === 0 ? (
          <p className="text-muted-foreground text-sm">None yet.</p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {notifications.map((item) => (
              <li key={item.id} className="space-y-0.5 px-3 py-2 text-sm">
                <div className="flex items-center gap-2">
                  <span className="rounded bg-muted px-1.5 py-0.5 text-xs">
                    {item.notification.kind}
                  </span>
                  <span className="font-medium">{item.notification.title}</span>
                  <span className="ml-auto text-muted-foreground text-xs">
                    {formatWhen(item.created_at)}
                  </span>
                </div>
                <div className="text-muted-foreground text-xs">
                  {item.status} ·{" "}
                  {Object.entries(item.results)
                    .map(([channel, result]) => `${channel}: ${result}`)
                    .join(" · ") || "waiting"}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
