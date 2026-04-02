export const AUTH_COOKIE_NAME = "compare_session";

function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) {
    out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return out === 0;
}

async function expectedToken(authSecret: string, password: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(authSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    enc.encode(`compare:${password}`),
  );
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function makeSessionCookieValue(
  authSecret: string,
  password: string,
): Promise<string> {
  return expectedToken(authSecret, password);
}

export async function verifySessionCookie(
  token: string | undefined,
  authSecret: string,
  password: string,
): Promise<boolean> {
  if (!token) return false;
  const expected = await expectedToken(authSecret, password);
  return timingSafeEqualHex(token, expected);
}
