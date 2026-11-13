import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { getPlans, startCheckout } from "./api";
import posthog, { isPostHogConfigured } from "./posthog";

function ModalPlanCard({ plan, credits, yearly, perClip, busy, current, onCredits,
  onBilling, onChoose }) {
  const tiers = plan.tiers || [];
  const tier = tiers.find((item) => item.credits === credits);
  const price = tier
    ? (yearly ? tier.yearly_usd : tier.monthly_usd)
    : (yearly ? plan.yearly_usd : plan.monthly_usd);
  const features = plan.features.filter((feature) => !feature.includes("clips a month"));

  return (
    <div className="quick-pricing-card">
      <div className="quick-plan-topline">
        <div>
          <strong>{plan.name}</strong>
          <span>{credits.toLocaleString()} credits every month</span>
        </div>
        <div className="quick-price">
          <strong>${price}</strong><span>/month</span>
          {yearly && <small className="plan-billed">billed ${(price * 12).toLocaleString()}/year</small>}
        </div>
      </div>

      <div className="billing-toggle" role="group" aria-label="Billing interval">
        <button type="button" className={!yearly ? "on" : ""}
                onClick={() => onBilling(false)} aria-pressed={!yearly}>Monthly</button>
        <button type="button" className={yearly ? "on" : ""}
                onClick={() => onBilling(true)} aria-pressed={yearly}>
          Yearly <span className="save">save 20%</span>
        </button>
      </div>

      <div className={`quick-credit-row ${tiers.length ? "" : "fixed"}`}
           role="group" aria-label={`${plan.name} monthly credits`}>
        {tiers.length ? tiers.map((item) => (
          <button key={item.credits} type="button"
                  className={credits === item.credits ? "selected" : ""}
                  onClick={() => onCredits(item.credits)}
                  aria-pressed={credits === item.credits}>
            <strong>{item.credits.toLocaleString()}</strong>
            <span>credits</span>
          </button>
        )) : (
          <div className="quick-fixed-credit">
            <strong>{plan.credits.toLocaleString()}</strong>
            <span>credits</span>
          </div>
        )}
      </div>

      <div className="quick-value">
        <strong>{Math.floor(credits / perClip).toLocaleString()} ready-to-post clips</strong>
        <span>with these monthly credits</span>
      </div>

      <ul className="quick-feature-list">
        {features.map((feature) => <li key={feature}><span aria-hidden="true">✓</span>{feature}</li>)}
      </ul>

      <button className="btn btn-primary btn-shine btn-block quick-cta"
              onClick={() => onChoose(plan)} disabled={current || busy === plan.id}>
        {current ? "Current plan" : busy === plan.id ? "Opening..." : `Choose ${plan.name}`}
      </button>
    </div>
  );
}

/**
 * Pricing section.
 *
 * Plans, tiers and prices all come from the backend, so the price on the card
 * and the price charged at checkout cannot drift apart. Paid plans carry a
 * credit dropdown: same plan, more credits per month, price follows.
 */
export default function Pricing({ user, onRequireAuth, modal = false, onClose, onViewAll }) {
  const [data, setData] = useState(null);
  const [yearly, setYearly] = useState(true);
  const [chosen, setChosen] = useState({});   // plan id -> selected credit amount
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [modalPlan, setModalPlan] = useState("creator");
  const [flipDirection, setFlipDirection] = useState(1);
  const reduceMotion = useReducedMotion();

  /* Priced by the server, so the page cannot quote a rate the backend would
     not actually charge. The fallback only covers the first paint. */
  const perClip = data?.credits_per_clip ?? 10;

  useEffect(() => {
    getPlans()
      .then((d) => {
        setData(d);
        // Start each plan on its headline tier.
        setChosen(Object.fromEntries(d.plans.map((p) => [p.id,
          p.id === "creator" ? 1000 : p.credits])));
      })
      .catch(() => {});
  }, []);

  async function choose(plan) {
    if (!user) return onRequireAuth?.();
    setBusy(plan.id);
    setNotice("");
    try {
      const r = await startCheckout({
        planId: plan.id,
        credits: chosen[plan.id],
        interval: yearly ? "yearly" : "monthly",
      });
      if (r.checkout_url) {
        if (isPostHogConfigured) {
          posthog.capture("checkout_started", {
            purchase_type: "subscription",
            plan: plan.id,
            billing_interval: yearly ? "yearly" : "monthly",
          });
        }
        setNotice("Opening secure checkout…");
        /* Remember what the account looked like BEFORE paying, so the return
           journey can tell "the payment landed" from "nothing happened yet" by
           comparing against it. Every cheaper signal is wrong for some real
           purchase: a top-up changes no plan and no entitlements, only the
           balance, and "has any entitlements at all" silently broke the moment
           Free gained its first one. sessionStorage because this must survive
           the round trip to Polar and no longer. */
        try {
          window.sessionStorage.setItem("billingBeforeCheckout", JSON.stringify({
            plan: user.plan || "free",
            credits: user.credits ?? 0,
          }));
        } catch { /* private mode: fall back to the plan check on return */ }
        window.location.assign(r.checkout_url);
        return;
      }
      setNotice("This payment option is not available yet. Please choose another one.");
    } catch (e) {
      setNotice(e.message);
    } finally {
      setBusy("");
    }
  }

  if (!data) return null;

  if (modal) {
    const paidPlans = data.plans.filter((plan) => plan.id === "creator" || plan.id === "pro");
    const activePlan = paidPlans.find((plan) => plan.id === modalPlan) || paidPlans[0];

    function showPlan(id) {
      if (id === modalPlan) return;
      setFlipDirection(id === "pro" ? 1 : -1);
      setModalPlan(id);
      setNotice("");
    }

    return (
      <div className="quick-pricing">
        <button className="upgrade-x" onClick={onClose} aria-label="Close pricing">×</button>
        <header className="quick-pricing-head">
          <span className="eyebrow">Plans and pricing</span>
          <h2>Choose the plan that fits how you create.</h2>
          <p>{perClip} credits makes one finished clip. Pick a monthly credit amount, then change it whenever you need.</p>
        </header>

        <div className="quick-plan-switch" role="tablist" aria-label="Choose a plan to view">
          {paidPlans.map((plan) => (
            <button key={plan.id} type="button" role="tab"
                    aria-selected={activePlan.id === plan.id}
                    className={activePlan.id === plan.id ? "selected" : ""}
                    onClick={() => showPlan(plan.id)}>
              <span>{plan.name}</span>
              <em>from ${yearly ? plan.yearly_usd : plan.monthly_usd}/mo</em>
            </button>
          ))}
        </div>

        <motion.div className="pricing-flip-drag"
                    drag={reduceMotion ? false : "x"}
                    dragConstraints={{ left: 0, right: 0 }} dragElastic={0.16}
                    onDragEnd={(_, info) => {
                      if (info.offset.x < -45) showPlan("pro");
                      if (info.offset.x > 45) showPlan("creator");
                    }}>
          <AnimatePresence mode="wait" initial={false} custom={flipDirection}>
            <motion.div key={activePlan.id} className="pricing-flip-card"
                        initial={reduceMotion ? { opacity: 0 } : {
                          opacity: 0, rotateY: flipDirection * 78, x: flipDirection * 22,
                        }}
                        animate={{ opacity: 1, rotateY: 0, x: 0 }}
                        exit={reduceMotion ? { opacity: 0 } : {
                          opacity: 0, rotateY: flipDirection * -78, x: flipDirection * -22,
                        }}
                        transition={{ type: "spring", stiffness: 260, damping: 27, mass: 0.8 }}
                        style={{ transformPerspective: 1200 }}>
              <ModalPlanCard
                plan={activePlan}
                credits={chosen[activePlan.id] ?? activePlan.credits}
                yearly={yearly}
                perClip={perClip}
                busy={busy}
                current={user?.plan === activePlan.id}
                onCredits={(credits) => setChosen({ ...chosen, [activePlan.id]: credits })}
                onBilling={setYearly}
                onChoose={choose}
              />
            </motion.div>
          </AnimatePresence>
        </motion.div>

        <div className="quick-pricing-foot">
          <span>Swipe or use the plan tabs to switch.</span>
          <button type="button" onClick={onViewAll}>Compare all plans</button>
        </div>
        {notice && <p className="pricing-notice">{notice}</p>}
      </div>
    );
  }

  return (
    <section className={`pricing ${modal ? "pricing-dialog-content" : ""}`} id={modal ? undefined : "pricing"}>
      <div className={modal ? "pricing-dialog-inner" : "container"}>
        <header className="pricing-head">
          <span className="eyebrow">Pricing</span>
          <h2>
            Pay for what you <span className="serif">actually clip.</span>
          </h2>
          {/* The differentiator, stated plainly. Everyone else meters the
              video you upload; a three-hour stream costs three hours whether
              you keep two clips or none. Here the source is free and you pay
              only for finished clips. */}
          <p><strong>{perClip} credits = 1 finished clip.</strong> Pay for the clips you post, not the length of the source video.</p>

          <div className="billing-toggle" role="group" aria-label="Billing interval">
            <button
              type="button"
              className={!yearly ? "on" : ""}
              onClick={() => setYearly(false)}
              aria-pressed={!yearly}
            >
              Monthly
            </button>
            <button
              type="button"
              className={yearly ? "on" : ""}
              onClick={() => setYearly(true)}
              aria-pressed={yearly}
            >
              Yearly <span className="save">save 20%</span>
            </button>
          </div>
        </header>

        <div className="plan-grid">
          {data.plans.map((plan) => {
            const tiers = plan.tiers || [];
            const credits = chosen[plan.id] ?? plan.credits;
            const tier = tiers.find((t) => t.credits === credits);
            const price = tier
              ? (yearly ? tier.yearly_usd : tier.monthly_usd)
              : (yearly ? plan.yearly_usd : plan.monthly_usd);
            const current = user?.plan === plan.id;
            const free = plan.monthly_usd === 0;

            return (
              <div key={plan.id} className={`plan ${plan.popular ? "popular" : ""}`}>
                {plan.popular && <span className="plan-badge">Most popular</span>}
                <h3>{plan.name}</h3>

                <div className="plan-price">
                  <strong>${price}</strong>
                  <span>{free ? "" : "/month"}</span>
                </div>
                {!free && (
                  <p className="plan-billed">
                    {yearly ? `Billed yearly · $${(price * 12).toLocaleString()}/year` : "Billed monthly · cancel anytime"}
                  </p>
                )}
                {tiers.length > 0 ? (
                  <div className="credit-picker" role="group" aria-label={`Credits per month on ${plan.name}`}>
                    <span className="credit-picker-label">Monthly credits</span>
                    <div className="credit-tier-grid">
                      {tiers.map((t) => (
                        <button key={t.credits} type="button"
                                className={credits === t.credits ? "selected" : ""}
                                onClick={() => setChosen({ ...chosen, [plan.id]: t.credits })}
                                aria-pressed={credits === t.credits}>
                          {t.credits.toLocaleString()}
                        </button>
                      ))}
                    </div>
                    <span className="credit-hint">
                      <strong>{Math.floor(credits / perClip).toLocaleString()} ready-to-post clips</strong> every month
                    </span>
                  </div>
                ) : (
                  <p className="credit-static">
                    {free
                      ? `${Math.floor(plan.credits / perClip).toLocaleString()} free clips on signup`
                      : `${plan.credits.toLocaleString()} credits · ${Math.floor(plan.credits / perClip).toLocaleString()} ready-to-post clips every month`}
                  </p>
                )}

                <button
                  className={`btn ${plan.popular ? "btn-primary" : "btn-ghost"} btn-block btn-sm`}
                  onClick={() => choose(plan)}
                  disabled={current || free || busy === plan.id}
                >
                  {current ? "Current plan"
                    : free ? "Included free"
                    : busy === plan.id ? "Opening…" : `Get ${plan.name}`}
                </button>

                <ul className="plan-features">
                  {plan.features.filter((f) => !f.includes("clips a month")).map((f) => (
                    <li key={f}>
                      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true">
                        <path d="m5 13 4 4L19 7" stroke="currentColor" strokeWidth="2.6"
                              strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                      {f}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>

        <div className="topups">
          <h4>Run out mid-month?</h4>
          <p className="topups-sub">
            Top up without changing your plan — these never expire.
          </p>
          <div className="topup-row">
            {data.topups.map((t) => (
              <div className="topup" key={t.credits}>
                <strong>{t.credits.toLocaleString()}</strong>
                <span>credits</span>
                <em>${t.usd}</em>
              </div>
            ))}
          </div>
        </div>

        {notice && <p className="pricing-notice">{notice}</p>}
      </div>
    </section>
  );
}
