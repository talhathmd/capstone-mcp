import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, makeSessionCookieValue } from "@/lib/auth";

export async function POST(request: Request) {
  const authSecret = process.env.AUTH_SECRET;
  const expected = process.env.COMPARE_PASSWORD;
  if (!authSecret || !expected) {
    return NextResponse.json(
      { error: "Server missing AUTH_SECRET or COMPARE_PASSWORD" },
      { status: 500 },
    );
  }

  let body: { password?: string };
  try {
    body = (await request.json()) as { password?: string };
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  if (body.password !== expected) {
    return NextResponse.json({ error: "Invalid password" }, { status: 401 });
  }

  const token = await makeSessionCookieValue(authSecret, expected);
  const store = await cookies();
  store.set(AUTH_COOKIE_NAME, token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 24 * 7,
  });

  return NextResponse.json({ ok: true });
}
