import { useCallback, useEffect, useState, type ReactNode } from "react";

import {
  ApiClientError,
  endBrowserSession,
  getAuthConfig,
  getBrowserSession,
  setCsrfToken,
  setUnauthorizedHandler,
} from "../api/client";
import { StatusBanner } from "../components/StatusBanner";

const LOGIN_PATH = "/api/v1/auth/login";

// Only these fixed messages are ever shown for the `auth_error` redirect parameter, so nothing from
// the URL (or the identity provider) is reflected into the page.
const REDIRECT_NOTICES = new Map<string, string>([
  ["login_failed", "Sign-in could not be completed. Please try again."],
  ["provider_unavailable", "The sign-in service is unavailable. Please try again shortly."],
]);
const EXPIRED_NOTICE = "Your session has expired. Please sign in again.";
const SIGNED_OUT_NOTICE = "You have been signed out.";

type AuthState =
  | { kind: "loading" }
  | { kind: "local" }
  | { kind: "signed-in" }
  | { kind: "signed-out"; loginAvailable: boolean; notice: string | null }
  | { kind: "error"; error: ApiClientError };

function readRedirectNotice(): string | null {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("auth_error");
  if (code === null) return null;
  // Remove the parameter so it is not kept in history, bookmarks, or screenshots.
  params.delete("auth_error");
  const query = params.toString();
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`,
  );
  return REDIRECT_NOTICES.get(code) ?? null;
}

function asClientError(error: unknown): ApiClientError {
  return error instanceof ApiClientError
    ? error
    : new ApiClientError("Unable to check your sign-in status. Try again.");
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ kind: "loading" });
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<ApiClientError | null>(null);

  const start = useCallback(async (initialNotice: string | null) => {
    try {
      const config = await getAuthConfig();
      if (config.mode === "local") {
        setState({ kind: "local" });
        return;
      }
      const session = await getBrowserSession();
      if (session === null) {
        setCsrfToken(null);
        setState({
          kind: "signed-out",
          loginAvailable: config.login_available,
          notice: initialNotice,
        });
        return;
      }
      setCsrfToken(session.csrf_token);
      setState({ kind: "signed-in" });
    } catch (caught) {
      setState({ kind: "error", error: asClientError(caught) });
    }
  }, []);

  useEffect(() => {
    const notice = readRedirectNotice();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- start() only sets state after awaiting the API
    void start(notice);
  }, [start]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setCsrfToken(null);
      setState((current) =>
        current.kind === "signed-in"
          ? { kind: "signed-out", loginAvailable: true, notice: EXPIRED_NOTICE }
          : current,
      );
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const signOut = async () => {
    setSigningOut(true);
    setSignOutError(null);
    try {
      await endBrowserSession();
      setCsrfToken(null);
      setState({ kind: "signed-out", loginAvailable: true, notice: SIGNED_OUT_NOTICE });
    } catch (caught) {
      setSignOutError(asClientError(caught));
    } finally {
      setSigningOut(false);
    }
  };

  if (state.kind === "loading") {
    return (
      <main id="main-content" tabIndex={-1} className="auth-screen" aria-busy="true">
        <p role="status">Checking sign-in status…</p>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main id="main-content" tabIndex={-1} className="auth-screen">
        <h1>Sign-in status unavailable</h1>
        <StatusBanner kind="error" requestId={state.error.requestId}>
          {state.error.message}
        </StatusBanner>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => {
            setState({ kind: "loading" });
            void start(null);
          }}
        >
          Try again
        </button>
      </main>
    );
  }

  if (state.kind === "signed-out") {
    return (
      <main id="main-content" tabIndex={-1} className="auth-screen">
        <h1>Conversational BI Workbench</h1>
        <p>Sign in to upload data, review anomalies, and ask grounded questions.</p>
        {state.notice ? <StatusBanner kind="info">{state.notice}</StatusBanner> : null}
        {state.loginAvailable ? (
          <a className="button button--primary" href={LOGIN_PATH}>
            Sign in
          </a>
        ) : (
          <StatusBanner kind="error">
            Sign-in is not configured for this deployment. Contact your administrator.
          </StatusBanner>
        )}
      </main>
    );
  }

  return (
    <>
      {state.kind === "signed-in" ? (
        <div className="auth-bar">
          <span>Signed in</span>
          <button
            className="button button--secondary button--compact"
            type="button"
            disabled={signingOut}
            onClick={() => void signOut()}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
          {signOutError ? (
            <StatusBanner kind="error" requestId={signOutError.requestId}>
              {signOutError.message}
            </StatusBanner>
          ) : null}
        </div>
      ) : null}
      {children}
    </>
  );
}
