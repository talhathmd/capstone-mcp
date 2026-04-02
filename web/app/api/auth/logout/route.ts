import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME } from "@/lib/auth";

export async function POST() {
  const store = await cookies();
  store.delete(AUTH_COOKIE_NAME);
  return NextResponse.json({ ok: true });
}
