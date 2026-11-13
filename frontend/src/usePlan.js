import { useEffect, useMemo, useState } from "react";
import { getPlans } from "./api";

/* What this visitor can reach, in one object.
 *
 * Entitlements and limits are decided by the server (billing.py) and mirrored
 * here for presentation only -- every gate is enforced again on the request.
 * The free ceilings come from /billing/plans rather than /auth/me because the
 * studio has to show them to someone who has not signed in yet.
 */

const FALLBACK = { max_clips: 2, max_source_minutes: 15, max_upload_mb: 100 };

export function usePlan(user) {
  const [catalogue, setCatalogue] = useState(null);

  useEffect(() => { getPlans().then(setCatalogue).catch(() => {}); }, []);

  return useMemo(() => {
    const entitlements = user?.entitlements || [];
    const limits = user?.limits || catalogue?.limits?.free || FALLBACK;
    // What the Free plan itself grants, straight from /billing/plans.
    const freeGrants = catalogue?.entitlements?.free || null;
    const can = (feature) => entitlements.includes(feature);
    return {
      plan: user?.plan || "free",
      /* Derived from what the account can actually DO, not from the plan name.
         The admin bypass (ADMIN_EMAILS) grants every entitlement while leaving
         the plan column at "free", so a plan-string check told admins their
         exports would be watermarked when the server had already decided they
         would not be. Any future comped account has the same shape.

         "Free" therefore means HOLDING NOTHING BEYOND WHAT FREE ITSELF GRANTS,
         which is not the same as holding nothing. Testing for an empty list
         worked only while Free had no capabilities at all, and every free
         account started reporting isFree === false the day it gained caption
         editing. Comparing against the server's own list for the free plan
         keeps this correct however the plans are rearranged next.

         Before the catalogue arrives, fall back to the watermark -- the one
         entitlement no free account has ever had. */
      isFree: freeGrants
        ? entitlements.every((feature) => freeGrants.includes(feature))
        : !entitlements.includes("no_watermark"),
      watermarked: !can("no_watermark"),
      isAdmin: !!user?.is_admin,
      limits,
      credits: user?.credits ?? null,
      creditsPerClip: catalogue?.credits_per_clip ?? 10,
      /* Surcharge rates from the server, so the studio quotes what billing.py
         will actually charge. The fallbacks only cover the first render before
         the catalogue lands; the pre-flight 402 is the real backstop if the
         two ever disagree. */
      highQualityPerClip: catalogue?.extras?.high_quality_per_clip ?? 5,
      advancedModelPerJob: catalogue?.extras?.advanced_model_per_job ?? 10,
      /* A highlight video is one deliverable priced by its finished length. */
      highlightPerMinute: catalogue?.extras?.highlight_per_minute ?? 5,
      highQualityPerHighlightMinute: catalogue?.extras?.high_quality_per_highlight_minute ?? 2,
      highlight: catalogue?.highlight ?? {
        min_source_minutes: 8, max_minutes: 15, choices: [5, 10, 15],
        auto_share: 0.15, min_auto_minutes: 3,
      },
      can,
    };
  }, [user, catalogue]);
}
