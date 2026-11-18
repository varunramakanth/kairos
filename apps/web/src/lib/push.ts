/** Browser side of Web Push: register the service worker and subscribe. */
import { subscribePush, unsubscribePush } from "@/client/sdk.gen";
import "@/lib/api";

export function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window
  );
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const registration = await navigator.serviceWorker.getRegistration("/sw.js");
  return (await registration?.pushManager.getSubscription()) ?? null;
}

/** Ask permission, subscribe this browser and register it with Kairos. */
export async function enablePush(
  vapidPublicKey: string,
): Promise<string | null> {
  if (!pushSupported())
    return "This browser does not support push notifications.";
  const permission = await Notification.requestPermission();
  if (permission !== "granted")
    return "Notifications are blocked for this site in your browser settings.";
  const registration = await navigator.serviceWorker.register("/sw.js");
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: keyBytes(vapidPublicKey),
  });
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys.auth)
    return "The browser returned an incomplete subscription.";
  const { error } = await subscribePush({
    body: {
      endpoint: json.endpoint,
      keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
    },
  });
  return error ? "Kairos could not save the subscription." : null;
}

export async function disablePush(): Promise<void> {
  const subscription = await currentSubscription();
  if (!subscription) return;
  await unsubscribePush({ body: { endpoint: subscription.endpoint } });
  await subscription.unsubscribe();
}
