"use client";

import { consent } from "@/services/consent/cookies";
import { environment } from "@/services/environment";
import { Analytics } from "@vercel/analytics/next";
import { SpeedInsights } from "@vercel/speed-insights/next";
import { useEffect, useState } from "react";

export function VercelAnalyticsWrapper() {
  const [hasAnalyticsConsent, setHasAnalyticsConsent] = useState(false);

  useEffect(() => {
    setHasAnalyticsConsent(consent.hasConsentFor("analytics"));
  }, []);

  // Vercel Web Analytics / Speed Insights are served only by Vercel's edge.
  // Off Vercel (Render, local Docker) the injected /_vercel/insights/script.js
  // 404s with no functional effect, so skip mounting them entirely there.
  if (!environment.isVercel()) {
    return null;
  }

  if (!hasAnalyticsConsent) {
    return null;
  }

  return (
    <>
      <SpeedInsights />
      <Analytics />
    </>
  );
}
