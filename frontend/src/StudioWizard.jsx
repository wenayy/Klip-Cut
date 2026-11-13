import { useEffect, useState } from "react";
import CaptionPresetPicker, { useCaptionPresets } from "./CaptionPresets";

/* The studio, as three deliberate steps.
 *
 * It used to be one screen where the source row, a collapsible "Output
 * settings" drawer and the Generate button all coexisted -- so the order of
 * operations was implied by vertical position, the settings were hidden behind a
 * chevron by default, and nothing ever confirmed what was about to run. Worse,
 * a free account met its ceilings as a rejected request AFTER pressing Generate.
 *
 * Source -> Preferences -> Review. Each step answers one question, the limits
 * are stated before they bite, and the last step shows the whole recipe and its
 * price before anything is spent.
 */

const STEPS = [
  { n: 1, id: "source", name: "Video", hint: "Add a link or file" },
  { n: 2, id: "prefs", name: "Make", hint: "Clips or a highlight video" },
  { n: 3, id: "review", name: "Review", hint: "Confirm and start" },
];

const LENGTHS = {
  any: "Any length", short: "Under 30s", medium: "30–60s",
  long: "60–90s", extended: "Over 90s",
};

/* What the moment picker hunts for. Auto is right for most videos; naming the
   genre is for the ones it cannot tell from the outside -- a gaming video with
   wall-to-wall commentary, or a podcast uploaded under a file name. */
const GENRES = [
  { value: "auto", label: "Auto", hint: "Detect from the video" },
  { value: "podcast", label: "Podcast", hint: "Insights, stories, sharp claims" },
  { value: "gaming", label: "Gaming", hint: "Clutches, fails, reactions" },
  { value: "general", label: "Other", hint: "Vlogs, essays, tutorials" },
];

function LockIcon() {
  return (
    <svg className="chip-lock" viewBox="0 0 24 24" width="13" height="13"
         fill="none" aria-hidden="true">
      <rect x="5" y="11" width="14" height="9" rx="2.2" stroke="currentColor" strokeWidth="2" />
      <path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3" stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" />
    </svg>
  );
}

/* One switch, one locked state, one place. Every gated boolean in step 2 renders
   through this so a locked control always behaves the same way: it is visible,
   it says why, and pressing it explains rather than doing nothing. */
function ToggleChip({ label, on, onChange, locked, lockReason, title, testid }) {
  return (
    <button
      type="button"
      className={`chip toggle ${on && !locked ? "active" : ""} ${locked ? "locked" : ""}`}
      onClick={() => (locked ? lockReason() : onChange(!on))}
      role={locked ? undefined : "switch"}
      aria-checked={locked ? undefined : on}
      title={locked ? "Upgrade to unlock — Creator includes 100 clips a month" : title}
      data-testid={testid}
    >
      {locked ? <LockIcon /> : <span className="chip-switch" aria-hidden="true" />}
      {label}
    </button>
  );
}

function StepRail({ step, maxReached, onGo }) {
  return (
    <ol className="wiz-rail" data-testid="wizard-rail">
      <span className="wiz-rail-fill"
            style={{ width: `${((step - 1) / (STEPS.length - 1)) * 100}%` }}
            aria-hidden="true" />
      {STEPS.map((s) => {
        const state = s.n < step ? "done" : s.n === step ? "now" : "todo";
        return (
          <li key={s.id} className={`wiz-step ${state}`}>
            <button type="button"
                    onClick={() => s.n <= maxReached && onGo(s.n)}
                    disabled={s.n > maxReached}
                    data-testid={`wizard-step-${s.id}`}>
              <span className="wiz-num" aria-hidden="true">
                {state === "done" ? (
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none">
                    <path d="m5 13 4 4L19 7" stroke="currentColor" strokeWidth="3"
                          strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                ) : s.n}
              </span>
              <span className="wiz-label">
                <strong>{s.name}</strong>
                <em>{s.hint}</em>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function Row({ label, value, muted }) {
  return (
    <div className={`review-row ${muted ? "muted" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ChoiceButtons({ label, value, options, onChange, className = "" }) {
  return (
    <div className={`choice-field ${className}`}>
      <span className="choice-label">{label}</span>
      <div className="choice-buttons" role="group" aria-label={label}>
        {options.map((option) => (
          <button key={option.value} type="button"
                  className={value === option.value ? "selected" : ""}
                  onClick={() => onChange(option)}
                  aria-pressed={value === option.value}
                  title={option.hint}
                  data-testid={option.testid}>
            {option.locked && <LockIcon />}
            <span>{option.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function StudioWizard({
  step, setStep, source, prefs, plan, templates, previewManifest,
  TemplatePreview, SourceTabs, DropZone, SourcePreview,
  submitting, submitError, onGenerate, onUpgrade, onViewPlans, uploadPct,
  signedIn, onRequireAuth,
}) {
  const [maxReached, setMaxReached] = useState(step);
  const captionData = useCaptionPresets();
  useEffect(() => { setMaxReached((m) => Math.max(m, step)); }, [step]);

  const { limits, can, isFree } = plan;
  const sourceReady = Boolean(source.verified);
  const sourceBlocked = Boolean(source.tooLong);
  const lockedTemplates = templates.filter((t) => t.id === "gameplay" && !can("gameplay"));
  // What is actually locked, rather than what the plan is called -- see usePlan.
  const anyLocked = lockedTemplates.length > 0
    || !can("tighten_pauses") || !can("multilingual");

  const clipOptions = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  const isHighlight = prefs.output === "highlight";
  const hl = plan.highlight;
  const sourceSeconds = Number(prefs.sourceSeconds) || 0;
  const highlightTooShort = sourceSeconds > 0 && sourceSeconds < hl.min_source_minutes * 60;
  /* Mirrors highlight.target_seconds: ~15% of the source, 3 to 15 minutes. */
  const autoMinutes = sourceSeconds
    ? Math.round(Math.max(hl.min_auto_minutes, Math.min(hl.max_minutes, (sourceSeconds * hl.auto_share) / 60)))
    : null;
  const highlightMinutes = prefs.highlightMinutes
    ? Math.min(prefs.highlightMinutes, sourceSeconds ? Math.floor((sourceSeconds * 0.6) / 60) || 1 : prefs.highlightMinutes)
    : (autoMinutes || hl.min_auto_minutes);
  /* Mirrors billing.credits_for_job: quality is per clip because a render runs
     once per clip, the model is per job because the analysis runs once however
     many clips come out of it. The rates come from the server (see usePlan) so
     only this shape is duplicated, not the numbers -- and the pre-flight 402 is
     what catches it if even the shape drifts. */
  /* Mirrors billing.credits_for_highlight for a highlight video: priced by its
     finished length, which is only known after the moments are chosen -- so
     this is the estimate the review step labels as one. */
  const costLines = isHighlight ? [{
    label: `Highlight video, about ${highlightMinutes} min`,
    detail: `${plan.highlightPerMinute} credits per minute`,
    credits: highlightMinutes * plan.highlightPerMinute,
  }] : [{
    label: `${prefs.nClips} clip${prefs.nClips === 1 ? "" : "s"}`,
    detail: `${plan.creditsPerClip} credits each`,
    credits: prefs.nClips * plan.creditsPerClip,
  }];
  if (prefs.highQuality) {
    costLines.push(isHighlight ? {
      label: "High quality",
      detail: `${plan.highQualityPerHighlightMinute} credits per minute`,
      credits: plan.highQualityPerHighlightMinute * highlightMinutes,
    } : {
      label: "High quality",
      detail: `${plan.highQualityPerClip} credits per clip`,
      credits: plan.highQualityPerClip * prefs.nClips,
    });
  }
  if (prefs.advancedModel) {
    costLines.push({
      label: "Advanced model",
      detail: "charged once per job",
      credits: plan.advancedModelPerJob,
    });
  }
  const cost = costLines.reduce((sum, line) => sum + line.credits, 0);

  /* The video size follows the product unless someone chose otherwise: a
     highlight video is a YouTube video, clips are vertical posts. */
  function chooseOutput(value) {
    if (value === "highlight" && highlightTooShort) return;
    prefs.setOutput(value);
    if (value === "highlight" && prefs.ratio === "9:16") prefs.setRatio("16:9");
    if (value === "clips" && prefs.ratio === "16:9") prefs.setRatio("9:16");
  }

  function next() {
    if (step === 1 && !sourceReady) return;
    if (step === 1 && !signedIn) { onRequireAuth(); return; }
    if (step === 1 && sourceBlocked) { onViewPlans(); return; }
    setStep(step + 1);
  }

  return (
    <div className="wizard" data-testid="studio-wizard">
      <StepRail step={step} maxReached={maxReached} onGo={setStep} />

      {/* ---------------- 1. Source ---------------- */}
      {step === 1 && (
        <section className="wiz-panel" data-testid="wizard-panel-source">
          <header className="wiz-head">
            <h3>Add the video you want to clip.</h3>
            <p>Paste a supported public link or upload a file from this device.</p>
          </header>

          <SourceTabs value={source.mode} onChange={source.setMode}
                      className="workspace-source-tabs" />

          {source.mode === "link" ? (
            <div className="workspace-urlrow">
              <input type="url" placeholder="Paste a public video link..."
                     value={source.url}
                     onChange={(e) => source.setUrl(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && sourceReady && next()}
                     aria-label="Public video URL"
                     data-testid="studio-url-input" />
            </div>
          ) : (
            <>
              <DropZone file={source.file} onFile={source.setFile} />
              <SourcePreview preview={null} loading={false} error=""
                             uploadUrl={source.uploadUrl}
                             uploadFile={source.file} onUploadMetadata={source.setUploadDuration} />
            </>
          )}

          {source.mode === "link" && (
            <SourcePreview preview={source.preview} loading={source.loading}
                           error={source.error} uploadUrl="" uploadFile={null}
                           onSwitchToUpload={() => source.setMode("upload")} />
          )}

          {sourceBlocked && (
            <div className="source-limit" role="status">
              <span>This video is longer than the {limits.max_source_minutes}-minute limit on your plan.</span>
              <button type="button" className="wiz-limits-link" onClick={onViewPlans}>
                See plans
              </button>
            </div>
          )}

          {/* Stated before it bites, not as a rejection after Generate. */}
          <p className="wiz-limits" data-testid="wizard-limits">
            Your plan: up to <strong>{limits.max_clips} clips</strong> per video,
            sources up to <strong>{limits.max_source_minutes} min</strong>
            {source.mode === "upload" && <>, uploads up to <strong>{limits.max_upload_mb} MB</strong></>}.
            {isFree && (
              <button type="button" className="wiz-limits-link" onClick={() => onUpgrade("clips")}
                      data-testid="limits-upgrade-link">
                Need more?
              </button>
            )}
          </p>
        </section>
      )}

      {/* ---------------- 2. Preferences ---------------- */}
      {step === 2 && (
        <section className="wiz-panel" data-testid="wizard-panel-prefs">
          <header className="wiz-head">
            <h3>What do you want to make?</h3>
            <p>{isHighlight
              ? "One video of the best parts of the whole thing, in the order they happened."
              : "Short clips that each stand on their own. You can still change each clip in the editor."}</p>
          </header>

          <div className="output-choice" role="radiogroup" aria-label="What to make">
            <button type="button" role="radio" aria-checked={!isHighlight}
                    className={`output-card ${!isHighlight ? "on" : ""}`}
                    onClick={() => chooseOutput("clips")} data-testid="opt-output-clips">
              <span className="output-shape clips" aria-hidden="true"><i /><i /><i /></span>
              <strong>Short clips</strong>
              <span>Vertical posts for TikTok, Reels and Shorts. Each one stands on its own.</span>
            </button>
            <button type="button" role="radio" aria-checked={isHighlight}
                    className={`output-card ${isHighlight ? "on" : ""} ${highlightTooShort ? "disabled" : ""}`}
                    onClick={() => chooseOutput("highlight")} disabled={highlightTooShort}
                    data-testid="opt-output-highlight">
              <span className="output-shape wide" aria-hidden="true"><i /></span>
              <strong>Highlight video</strong>
              <span>{highlightTooShort
                ? `Needs a video of at least ${hl.min_source_minutes} minutes.`
                : `One ${autoMinutes ? `~${autoMinutes}-minute ` : ""}video of the best parts, for YouTube.`}</span>
            </button>
          </div>

          <div className="pref-group">
            <span className="pref-group-label">{isHighlight ? "Highlight setup" : "Clip setup"}</span>
            <div className="choice-stack">
              {isHighlight && (
                <ChoiceButtons
                  label="Length"
                  value={prefs.highlightMinutes ?? "auto"}
                  options={[
                    { value: "auto", label: autoMinutes ? `Auto · ~${autoMinutes} min` : "Auto",
                      testid: "opt-hl-auto" },
                    /* A length the source cannot fill is not offered at all. */
                    ...hl.choices
                      .filter((m) => !sourceSeconds || m * 60 <= sourceSeconds * 0.6)
                      .map((m) => ({ value: m, label: `${m} min`, testid: `opt-hl-${m}` })),
                  ]}
                  onChange={(option) =>
                    prefs.setHighlightMinutes(option.value === "auto" ? null : option.value)}
                />
              )}
              {!isHighlight && <ChoiceButtons
                label="Number of clips"
                value={prefs.nClips}
                className="clip-count-choice"
                options={clipOptions.map((n) => ({
                  value: n,
                  label: n,
                  locked: n > limits.max_clips,
                  testid: `opt-nclips-${n}`,
                }))}
                onChange={(option) => {
                  if (option.locked) { onUpgrade("clips"); return; }
                  prefs.setNClips(option.value);
                }}
              />}
              <ChoiceButtons
                label="Video size"
                value={prefs.ratio}
                options={[
                  { value: "9:16", label: "Vertical 9:16", testid: "opt-ratio-vertical" },
                  { value: "1:1", label: "Square 1:1", testid: "opt-ratio-square" },
                  { value: "16:9", label: "Wide 16:9", testid: "opt-ratio-wide" },
                ]}
                onChange={(option) => prefs.setRatio(option.value)}
              />
              <ChoiceButtons
                label="Content type"
                value={prefs.genre}
                options={GENRES.map((g) => ({
                  value: g.value, label: g.label, hint: g.hint,
                  testid: `opt-genre-${g.value}`,
                }))}
                onChange={(option) => prefs.setGenre(option.value)}
              />
              {!isHighlight && (
                <ChoiceButtons
                  label="Clip length"
                  value={prefs.lengthPref}
                  options={Object.entries(LENGTHS).map(([value, label]) => ({ value, label }))}
                  onChange={(option) => prefs.setLengthPref(option.value)}
                />
              )}
            </div>
          </div>

          <div className="pref-group">
            <span className="pref-group-label">Captions and audio</span>
            <div className="options-row">
              <ToggleChip label="Captions" on={prefs.burnSubtitles}
                          onChange={prefs.setBurnSubtitles} testid="opt-captions"
                          title="Word-timed captions burned into the video" />
              {!prefs.burnSubtitles && !isHighlight && (
                <ToggleChip label="Keywords + emoji" on={prefs.keywordCaptions}
                            onChange={prefs.setKeywordCaptions} testid="opt-keyword-captions"
                            title="No full captions, but the key phrases still pop up with an emoji" />
              )}
              {!isHighlight && (
                <ToggleChip label="Title" on={prefs.burnTitle}
                            onChange={prefs.setBurnTitle} testid="opt-title"
                            title="Burn a title card into the first few seconds of each clip" />
              )}
              <ToggleChip label="Auto-censor" on={prefs.autoCensor}
                          onChange={prefs.setAutoCensor} testid="opt-censor"
                          title="Mutes profanity and stars it in captions, so clips stay monetisable" />
              <ToggleChip label="Mixed language" on={prefs.multilingual}
                          onChange={prefs.setMultilingual}
                          locked={!can("multilingual")}
                          lockReason={() => onUpgrade("multilingual")}
                          testid="opt-multilingual"
                          title="Transcribes speech that switches language mid-sentence" />
            </div>
          </div>

          {/* Priced in credits, not locked behind a plan -- so these carry a
              cost badge rather than a padlock, and the total they feed sits on
              the Review step where it is agreed to before anything is spent. */}
          <div className="pref-group">
            <span className="pref-group-label">Output quality</span>
            <div className="options-row">
              <ToggleChip label={isHighlight
                            ? `High quality  +${plan.highQualityPerHighlightMinute}/min`
                            : `High quality  +${plan.highQualityPerClip}/clip`}
                          on={prefs.highQuality}
                          onChange={prefs.setHighQuality}
                          testid="opt-high-quality"
                          title="Sharper picture: a slower encode that keeps more detail. Does not change which moments are chosen. Most platforms recompress the difference away, so this is for footage you will re-cut or archive." />
              <ToggleChip label={`Advanced model  +${plan.advancedModelPerJob}`}
                          on={prefs.advancedModel}
                          onChange={prefs.setAdvancedModel}
                          testid="opt-advanced-model"
                          title="Better choices: a stronger model reads the transcript and decides which moments to cut and where each one starts and ends. Does not change picture quality. Charged once per job, however many clips it finds." />
            </div>
          </div>

          <div className="pref-group">
            <span className="pref-group-label">Pacing</span>
            <div className="options-row">
              <ToggleChip label="Tighten pauses" on={prefs.tightenPauses}
                          onChange={prefs.setTightenPauses}
                          locked={!can("tighten_pauses")}
                          lockReason={() => onUpgrade("tighten_pauses")}
                          testid="opt-tighten"
                          title="Cuts the dead air so a clip feels edited rather than trimmed" />
              {isHighlight && (
                <>
                  <ToggleChip label="Meme moments" on={prefs.highlightMemes}
                              onChange={prefs.setHighlightMemes} testid="opt-hl-memes"
                              title="A quick reaction beat after the biggest moments: a freeze, a punch-in, a caption and a sound" />
                  <ToggleChip label="Background music" on={prefs.highlightMusic}
                              onChange={prefs.setHighlightMusic} testid="opt-hl-music"
                              title="A royalty-free music bed under the whole video, ducked whenever someone talks" />
                  <ToggleChip label="Time-jump tags" on={prefs.highlightTags}
                              onChange={prefs.setHighlightTags} testid="opt-hl-tags"
                              title={'"25 MIN LATER" where the video skips ahead, so viewers keep their place'} />
                </>
              )}
            </div>
          </div>

          {/* A wide highlight video keeps the original picture; every other
              output is framed by a layout. */}
          {!(isHighlight && prefs.ratio === "16:9") && (
          <div className="pref-group">
            <span className="pref-group-label">Layout</span>
            <div className="template-grid">
              {templates.map((t) => {
                const locked = lockedTemplates.includes(t);
                const selected = prefs.template === t.id && !locked;
                return (
                  <button
                    type="button"
                    key={t.id}
                    className={`template-card ${selected ? "selected" : ""} ${locked ? "locked" : ""}`}
                    onClick={() => (locked ? onUpgrade("gameplay") : prefs.setTemplate(t.id))}
                    aria-pressed={selected}
                    data-testid={`opt-template-${t.id}`}
                  >
                    <TemplatePreview template={t} variants={previewManifest[t.id]} />
                    <span className="template-name">
                      {t.name}
                      {locked && <LockIcon />}
                    </span>
                    <span className="template-note">
                      {locked ? "Upgrade to unlock" : t.note}
                    </span>
                    {selected && <span className="template-tick">✓</span>}
                  </button>
                );
              })}
            </div>
          </div>
          )}

          {(prefs.burnSubtitles || (prefs.keywordCaptions && !isHighlight)) && (
            <div className="pref-group caption-pref-group">
              <span className="pref-group-label">Caption style</span>
              <p className="pref-help">{prefs.burnSubtitles
                ? "This style will be used in the generated video. Paid plans can change it later in the editor."
                : "The keyword pop-ups use this style. Paid plans can change it later in the editor."}</p>
              <CaptionPresetPicker value={prefs.captionStyle}
                                   onChange={prefs.setCaptionStyle}
                                   data={captionData} />
            </div>
          )}

          <label className="intent-field">
            <span className="intent-heading">
              <span className="intent-label">Looking for a specific moment?</span>
              <span className="intent-optional">Optional</span>
            </span>
            <span className="intent-help">
              Describe it here. KlipCut will prioritise one strong match and still choose the best moments overall.
            </span>
            <input type="text" value={prefs.intent} data-testid="opt-intent"
                   onChange={(e) => prefs.setIntent(e.target.value)}
                   placeholder="For example: when they explain how pricing works" />
          </label>

          {/* One strip, at the end of the step. Not a banner per locked control:
              that reads as nagging, and nagging is what makes people leave. */}
          {anyLocked && (
            <div className="unlock-strip" data-testid="unlock-strip">
              <span>
                <strong>Upgrade to unlock gameplay, pause tightening and mixed-language captions.</strong>
                <em>Creator includes 100 finished clips each month.</em>
              </span>
              <button className="btn btn-primary btn-sm btn-shine"
                      onClick={onViewPlans} data-testid="unlock-strip-btn">
                View plans
              </button>
            </div>
          )}
        </section>
      )}

      {/* ---------------- 3. Review ---------------- */}
      {step === 3 && (
        <section className="wiz-panel" data-testid="wizard-panel-review">
          <header className="wiz-head">
            <h3>Ready to generate.</h3>
            <p>This is what will run. Nothing is spent until you press the button.</p>
          </header>

          <div className="review-card">
            <Row label="Source" value={source.mode === "upload"
              ? (source.file?.name || "Uploaded file")
              : (source.preview?.title || source.url)} />
            {isHighlight
              ? <Row label="Make" value={`Highlight video · about ${highlightMinutes} min`} />
              : <Row label="Make" value={`${prefs.nClips} clip${prefs.nClips === 1 ? "" : "s"} · ${LENGTHS[prefs.lengthPref].toLowerCase()}`} />}
            <Row label="Content" value={GENRES.find((g) => g.value === prefs.genre)?.label
              + " · " + GENRES.find((g) => g.value === prefs.genre)?.hint.toLowerCase()} />
            <Row label="Format" value={isHighlight && prefs.ratio === "16:9"
              ? "16:9 · original picture"
              : `${prefs.ratio} · ${templates.find((t) => t.id === prefs.template)?.name || prefs.template}`} />
            <Row label="Captions" value={prefs.burnSubtitles
              ? (captionData?.presets?.find((p) => p.id === prefs.captionStyle)?.name || "On")
              : prefs.keywordCaptions && !isHighlight ? "Keywords + emoji only" : "Off"} />
            {!isHighlight && <Row label="Title" value={prefs.burnTitle ? "On" : "Off"} />}
            <Row label="Auto-censor" value={prefs.autoCensor ? "On" : "Off"} />
            <Row label="Tighten pauses"
                 value={prefs.tightenPauses && can("tighten_pauses") ? "On" : "Off"}
                 muted={!can("tighten_pauses")} />
            <Row label="Mixed language"
                 value={prefs.multilingual && can("multilingual") ? "On" : "Off"}
                 muted={!can("multilingual")} />
            {prefs.intent.trim() && <Row label="Looking for" value={prefs.intent.trim()} />}
            {isHighlight && <Row label="Meme moments" value={prefs.highlightMemes ? "On" : "Off"} />}
            {isHighlight && <Row label="Time-jump tags" value={prefs.highlightTags ? "On" : "Off"} />}
            {isHighlight && <Row label="Background music" value={prefs.highlightMusic ? "On" : "Off"} />}
            <Row label="Quality" value={prefs.highQuality ? "High" : "Standard"} />
            <Row label="Model" value={prefs.advancedModel ? "Advanced" : "Standard"} />

            {/* Itemised only when something was added to the base rate. With no
                extras this is one line restating the total, which is noise --
                and the extras are the whole reason the total is not simply
                clips x 10, so they are what has to be shown. */}
            {costLines.length > 1 && (
              <div className="review-costlines" data-testid="cost-breakdown">
                {costLines.map((line) => (
                  <div className="review-costline" key={line.label}>
                    <span>{line.label}<em>{line.detail}</em></span>
                    <strong>{line.credits}</strong>
                  </div>
                ))}
              </div>
            )}
            <Row label="Cost" value={(isHighlight ? "about " : "") + (plan.credits == null
              ? `${cost} credits`
              : `${cost} of your ${plan.credits} credits`)} />
            {isHighlight && (
              <p className="review-fineprint">
                Charged by the finished length once the best parts are chosen.
              </p>
            )}
          </div>

          {plan.watermarked && (
            <div className="review-note" data-testid="watermark-note">
              <span>
                Free clips carry a small <strong>Made with KlipCut</strong> credit at the
                foot of the frame.
              </span>
              <button type="button" className="wiz-limits-link"
                      onClick={() => onUpgrade("no_watermark")}
                      data-testid="watermark-upgrade-link">
                Remove it
              </button>
            </div>
          )}

          {submitError && <div className="error-box">{submitError}</div>}
        </section>
      )}

      <footer className="wiz-nav">
        <button className="btn btn-ghost btn-sm" data-testid="wizard-back"
                onClick={() => setStep(Math.max(1, step - 1))}
                disabled={step === 1 || submitting}>
          Back
        </button>
        <span className="wiz-nav-count">Step {step} of {STEPS.length}</span>
        {step < 3 ? (
          <button className="btn btn-primary btn-shine" onClick={next}
                  disabled={step === 1 && (!sourceReady || source.loading || sourceBlocked)}
                  title={step === 1 && (!sourceReady || source.loading || sourceBlocked)
                    ? (sourceBlocked ? "Choose a plan for longer videos"
                      : source.loading ? "Checking the video"
                      : source.mode === "upload" ? "Choose a video file first"
                                                : "Paste a video link first")
                    : undefined}
                  data-testid="wizard-next">
            {step === 1 ? (signedIn ? "Choose preferences" : "Sign in to continue") : "Review"}
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" aria-hidden="true">
              <path d="M5 12h14m0 0-6-6m6 6-6 6" stroke="currentColor" strokeWidth="2.2"
                    strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        ) : (
          <div className="wiz-generate">
            <button className="btn btn-primary btn-shine" onClick={onGenerate}
                    disabled={submitting} data-testid="wizard-generate"
                    data-uploading={uploadPct != null ? "" : undefined}>
              {uploadPct == null
                ? (submitting ? "Starting..."
                   : isHighlight ? "Make highlight video"
                   : `Generate ${prefs.nClips} clip${prefs.nClips === 1 ? "" : "s"}`)
                : uploadPct >= 1 ? "Processing..."
                /* -1 means the browser could not size the body. Running the
                   percentage arithmetic on it would render "Uploading -100%". */
                : uploadPct < 0 ? "Uploading..."
                : `Uploading ${Math.round(uploadPct * 100)}%`}
            </button>

            {uploadPct != null && (
              <div className="upload-progress" data-testid="upload-progress"
                   role="status" aria-live="polite">
                <div className="upload-bar"
                     data-indeterminate={uploadPct < 0 ? "" : undefined}
                     role="progressbar" aria-valuemin={0} aria-valuemax={100}
                     aria-valuenow={uploadPct >= 0 ? Math.round(uploadPct * 100) : undefined}>
                  <span style={{ width: `${Math.round(Math.max(uploadPct, 0) * 100)}%` }} />
                </div>
                {/* Says what governs the wait, honestly. This leg is the file
                    leaving THIS device, so it is bound by the upstream link --
                    not by the plan, and not by anything a faster server would
                    help with. Blaming the plan here would be a lie that costs
                    trust exactly when someone is watching a slow bar. */}
                <p className="upload-note">
                  {uploadPct >= 1
                    ? "Upload complete — checking the video before we start."
                    : "Sending from this device. Speed depends on your connection, not your plan."}
                </p>
              </div>
            )}
          </div>
        )}
      </footer>
    </div>
  );
}
