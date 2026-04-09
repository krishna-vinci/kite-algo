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

export async function apiFetch<T>(
  input: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const { json, baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "", headers, ...init } = options;
  const response = await fetch(`${baseUrl}${input}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(json ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: json === undefined ? init.body : JSON.stringify(json),
  });

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
