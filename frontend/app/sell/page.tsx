"use client";

import Link from "next/link";
import { useAuth } from "@/components/AuthProvider";
import ListingUploader from "@/components/ListingUploader";

export default function SellPage() {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user)
    return (
      <div className="empty">
        <h1>List your property for free</h1>
        <p className="muted">Sign in with your mobile number so buyers' enquiries and visit bookings reach you.</p>
        <Link className="button" href="/login?next=/sell">Sign in to continue</Link>
      </div>
    );
  return <ListingUploader />;
}
