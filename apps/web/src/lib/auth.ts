/**
 * Sign-in for the deployed app: Amazon Cognito Hosted UI, authorization code + PKCE
 * (oidc-client-ts). Locally the API reports auth as disabled and nothing here runs.
 *
 * The API serves the settings at /auth/config, so the same build works for any
 * deployment. The access token is attached to every API request, including the SSE
 * streams, by one request interceptor on the generated client.
 */
import { UserManager, WebStorageStateStore } from "oidc-client-ts";
import { client } from "@/client/client.gen";
import { authConfig } from "@/client/sdk.gen";
import type { AuthConfig } from "@/client/types.gen";

export const CALLBACK_PATH = "/auth/callback";

let manager: UserManager | null = null;
let settings: AuthConfig | null = null;

/** Fetch the auth settings once and set up the token interceptor if auth is on. */
export async function initAuth(): Promise<AuthConfig> {
  if (settings) return settings;
  const { data } = await authConfig();
  settings = data ?? { enabled: false };
  if (settings.enabled && settings.issuer && settings.client_id) {
    manager = new UserManager({
      authority: settings.issuer,
      client_id: settings.client_id,
      redirect_uri: `${window.location.origin}${CALLBACK_PATH}`,
      post_logout_redirect_uri: `${window.location.origin}/`,
      response_type: "code",
      scope: "openid email",
      automaticSilentRenew: true,
      userStore: new WebStorageStateStore({ store: window.localStorage }),
    });
    client.interceptors.request.use(async (request) => {
      const token = await accessToken();
      if (token) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    });
  }
  return settings;
}

/** A valid access token, refreshing it if it has expired; null when signed out. */
export async function accessToken(): Promise<string | null> {
  if (!manager) return null;
  let user = await manager.getUser();
  if (user?.expired && user.refresh_token) {
    try {
      user = await manager.signinSilent();
    } catch {
      user = null;
    }
  }
  return user && !user.expired ? user.access_token : null;
}

export async function signIn(): Promise<void> {
  await manager?.signinRedirect({ state: window.location.pathname });
}

/** Finish the redirect back from Cognito; returns where the user was going. */
export async function completeSignIn(): Promise<string> {
  if (!manager) return "/";
  const user = await manager.signinRedirectCallback();
  return typeof user.state === "string" && user.state.startsWith("/")
    ? user.state
    : "/";
}

/** Clear the local session, then end the Cognito session on its logout page. */
export async function signOut(): Promise<void> {
  if (!manager || !settings?.domain || !settings.client_id) return;
  await manager.removeUser();
  const params = new URLSearchParams({
    client_id: settings.client_id,
    logout_uri: `${window.location.origin}/`,
  });
  window.location.assign(`${settings.domain}/logout?${params}`);
}

export function authEnabled(): boolean {
  return Boolean(settings?.enabled);
}
