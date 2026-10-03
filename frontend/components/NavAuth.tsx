"use client";

import Link from "next/link";
import { useAuth } from "./AuthProvider";

export default function NavAuth() {
  const { user, loading, signOut } = useAuth();
  if (loading) return null;
  if (!user) return <Link href="/login">Sign in</Link>;
  return (
    <>
      <span className="muted small">{user.full_name ?? user.phone_e164}</span>
      <button className="ghost" onClick={signOut}>Sign out</button>
    </>
  );
}
