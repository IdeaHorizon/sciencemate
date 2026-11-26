"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// Older installations may have saved this settings URL as their last page.
export default function RetiredCnkiSettings() {
  const router = useRouter();
  useEffect(() => { router.replace("/settings"); }, [router]);
  return null;
}
