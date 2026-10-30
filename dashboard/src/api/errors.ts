/**
 * OBS-08: the one error type every page distinguishes on --
 * `docs/LOW_LEVEL_IMPLEMENTATION.md`: "A 401 prompts sign-in; a 403 shows
 * access denied; a 404 on a detail route shows a resource-not-found
 * state" -- plus a `network` kind for the case none of those map to: the
 * request never reached a server at all (DNS/connection failure, or the
 * dev proxy's own target is down). `docs/dashboard.md`: "Dashboard/API
 * outages show a recoverable error rather than silently empty charts."
 */

export type ApiErrorKind = "unauthenticated" | "forbidden" | "not_found" | "server" | "network";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;

  constructor(kind: ApiErrorKind, message: string, status: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

export function apiErrorKindForStatus(status: number): ApiErrorKind {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  return "server";
}
