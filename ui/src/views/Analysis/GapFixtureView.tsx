import { useState } from "react";

import gaps from "../../../test/fixtures/gaps.json";
import type { Analysis, ComplianceResult } from "../../api/client";
import { Banner } from "../../components/Banner";
import { MetricTile } from "../../components/MetricTile";
import { PageHead } from "../../components/PageHead";
import { StateChip } from "../../components/StateChip";
import { useCriteria } from "../../hooks/useCriteria";
import { CriterionRow } from "./CriterionRow";
import { needsReviewCount, overallState, quoteCounts } from "./overall";
import styles from "./AnalysisView.module.css";

/**
 * The gap states, rendered from a fixture. **Development only.**
 *
 * The sample contract comes back all-green, so `Partially Compliant`,
 * `Non-Compliant`, the `partial` / `missing` / `not_determined` markers, an
 * unverified quote and `needs_review` are all unreachable from real data. A UI
 * verified only against the sample has not been verified, and these are
 * exactly the states that matter most -- an unverified quote is the
 * hallucination-detection story made visible.
 *
 * The route is `/_gaps`, guarded by `import.meta.env.DEV` where it is
 * registered, so it is not in the production bundle. The fixture is imported
 * from the test tree for the same reason it lives there: it is constructed,
 * and it must never be mistaken for the output of a run.
 */
export function GapFixtureView() {
  const criteria = useCriteria();
  const [open, setOpen] = useState<string | null>("password_management");
  const report = fixture.report;
  const results = (report?.results ?? []) as ComplianceResult[];

  return (
    <>
      <PageHead title="Gap states" subtitle="A constructed fixture. Not the output of any run." />
      <div className={styles.page}>
        <Banner
          tone="warn"
          title="This report is fabricated."
          hint="Statuses were altered by hand to reach the states the sample contract never produces. It exists to be looked at, and it never leaves the test tree."
        />

        <div className={styles.tiles}>
          <MetricTile label="Overall" value={<StateChip state={overallState(results)} />} />
          <MetricTile label="Mean confidence" value={(report?.totals?.mean_confidence ?? 0).toFixed(2)} />
          <MetricTile
            label="Quotes verified"
            value={`${quoteCounts(results).verified} / ${quoteCounts(results).total}`}
          />
          <MetricTile label="Needs review" value={String(needsReviewCount(results))} />
        </div>

        <div className={styles.criteria}>
          {results.map((result, index) => (
            <CriterionRow
              key={result.criterion_id}
              index={index + 1}
              result={result}
              criterion={criteria.data?.find((c) => c.id === result.criterion_id)}
              open={open === result.criterion_id}
              onToggle={() =>
                setOpen((current) => (current === result.criterion_id ? null : result.criterion_id))
              }
            />
          ))}
        </div>
      </div>
    </>
  );
}

const fixture = { report: gaps } as unknown as Analysis;
