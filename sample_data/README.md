# Synthetic demo assets

All files in this directory are synthetic and contain no customer or personal data.

The primary portfolio scenario is a retail-operations review workflow:

- `retail_operations.csv` contains 2,400 deterministic transactions across regions,
  categories, channels, and 18 months.
- `retail_operations_policy.pdf` defines the review and fulfillment thresholds used by
  document and hybrid questions.
- `retail_operations_ground_truth.json` records exact, machine-readable answers calculated
  from the CSV plus the document-only policy answer.

Regenerate all three files with:

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.generate_retail_demo
```

The generator uses a fixed seed, decimal arithmetic, stable CSV line endings, and ReportLab's
invariant PDF mode. Tests compare regenerated files byte-for-byte with the committed artifacts.
The `status=review` field means that at least one documented rule matched; it is not a label for
fraud, misconduct, or causation.

The smaller legacy files remain available for existing regression tests and quick smoke checks.

## Subscription churn investigation fixture

`subscription_churn.csv` contains 8,200 synthetic customer-quarter records from 2025-Q3 through
2026-Q2. Four accompanying PDFs provide a pricing notice, a support operations update, a retention
playbook, and a deliberately unreliable draft board memo. The memo contains one malicious sentence
to exercise deterministic untrusted-document boundaries; it is fixture content, not an instruction.

Regenerate the source assets and evaluator-only files with:

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.generate_subscription_churn
& ".\.venv\Scripts\python.exe" -m scripts.generate_subscription_churn --check
```

The generator uses seed `20261013`, stable customer identifiers, LF-delimited text, decimal
currency, and ReportLab invariant mode. Customers retain their segment, plan, region, and original
start date across quarters. Tenure is the number of calendar-month boundaries from that start date
to the quarter start; cancellation or reactivation does not reset it. Survivors and reactivations
become eligible in the next quarter, while same-segment and same-plan new customers replace the
remaining churn losses. All records and documents are invented. The evaluator-only oracle and
reference SQL live under `evals/investigation/v1/`; they must never be uploaded, indexed, or passed
to a model.
