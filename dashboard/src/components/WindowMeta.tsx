/**
 * OBS-08: `docs/dashboard.md`: "Every chart states time window, units,
 * data freshness, sample count and active filters." One shared renderer
 * so no page can forget one of these.
 */

export function WindowMeta({
  since,
  until,
  asOf,
  sampleCount,
}: {
  since: string;
  until: string;
  asOf: string;
  sampleCount: number;
}) {
  return (
    <p className="window-meta">
      Window: {since} &ndash; {until} &middot; as of {asOf} &middot; sample size: {sampleCount}
    </p>
  );
}
