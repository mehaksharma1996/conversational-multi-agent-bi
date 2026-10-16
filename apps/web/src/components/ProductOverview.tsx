/**
 * What the product is for, what it does with data, and where it stops. Every statement here
 * describes behavior enforced in code and covered by the repository's tests and evaluations;
 * nothing claims accuracy, autonomy, or the absence of model error.
 */
export function ProductOverview() {
  return (
    <section className="product-overview" aria-labelledby="product-overview-heading">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Review-first analytics</p>
          <h2 id="product-overview-heading">Evidence stays attached to the answer</h2>
        </div>
        <p className="section-lede">
          Numbers come from validated read-only SQL and deterministic analysis. Model-backed
          answers retain their query, result rows, sources, and review limits.
        </p>
      </div>
      <div className="overview-grid">
        <article className="overview-card">
          <h3>What it solves</h3>
          <p>
            Analysts spend time writing queries, cross-reading policy documents, and explaining
            where a figure came from. This workbench drafts the query, retrieves the policy
            passage, and keeps both next to the result.
          </p>
        </article>
        <article className="overview-card">
          <h3>What it analyzes</h3>
          <p>
            CSV and Excel tables (profiled, schema-reviewed by you) and text-based PDFs. Try the
            built-in synthetic retail data below; nothing in it is real customer data.
          </p>
        </article>
        <article className="overview-card">
          <h3>What you can ask</h3>
          <p>
            Rankings, comparisons, trends, and counts over the table; what a policy says; and
            hybrid questions such as how many transactions meet a documented rule.
          </p>
        </article>
        <article className="overview-card">
          <h3>How answers are grounded</h3>
          <p>
            Figures are computed from the query result by code, not written by a model. Document
            answers cite retrieved passages and quotations are checked against them.
          </p>
        </article>
        <article className="overview-card">
          <h3>Safety controls</h3>
          <p>
            One read-only SELECT, allowlisted tables and functions, a row cap and time limit,
            optional approval before SQL runs, consent before data reaches a hosted model, and
            per-workspace isolation with bounded retention.
          </p>
        </article>
        <article className="overview-card overview-card--limits">
          <h3>Limits to keep in mind</h3>
          <p>
            A model drafts the SQL, so it can answer a slightly different question than you
            meant: read the query. Citation checks confirm structure, not truth. Results show
            association, not cause. This is decision support, not an automated decision.
          </p>
        </article>
      </div>
    </section>
  );
}
