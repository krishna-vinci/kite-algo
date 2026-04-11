export type ApiFetchOptions = RequestInit & {
  json?: unknown;
  baseUrl?: string;
};

export class ApiClientError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message = `Request failed with status ${status}`,
  ) {
    super(message);
    this.name = "ApiClientError";
  }
}

let refreshPromise: Promise<void> | null = null;

function redirectToLogin() {
  if (typeof window === "undefined") {
    return;
  }
  if (window.location.pathname === "/login") {
    return;
  }
  const next = `${window.location.pathname}${window.location.search}`;
  const loginUrl = new URL("/login", window.location.origin);
  if (next && next !== "/") {
    loginUrl.searchParams.set("next", next);
  }
  window.location.assign(loginUrl.toString());
}

async function refreshAppSession() {
  if (!refreshPromise) {
    refreshPromise = fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json" },
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error("Session refresh failed");
        }
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

export async function apiFetch<T>(
  input: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const defaultBaseUrl =
    typeof window === "undefined"
      ? (process.env.NEXT_PUBLIC_API_BASE_URL ?? "")
      : "";
  const { json, baseUrl = defaultBaseUrl, headers, ...init } = options;
  const requestInit: RequestInit = {
    ...init,
    credentials: init.credentials ?? "include",
    headers: {
      Accept: "application/json",
      ...(json ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: json === undefined ? init.body : JSON.stringify(json),
  };

  let response = await fetch(`${baseUrl}${input}`, requestInit);

  if (response.status === 401 && !input.includes("/auth/login") && !input.includes("/auth/refresh")) {
    try {
      await refreshAppSession();
      response = await fetch(`${baseUrl}${input}`, requestInit);
    } catch {
      redirectToLogin();
      // let the original 401 handling below raise a typed error
    }
  }

  const text = await response.text();
  const body = text ? safeParseJson(text) : null;

  if (!response.ok) {
    throw new ApiClientError(response.status, body, response.statusText || `Request failed with status ${response.status}`);
  }

  return body as T;
}

function safeParseJson(value: string) {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}
