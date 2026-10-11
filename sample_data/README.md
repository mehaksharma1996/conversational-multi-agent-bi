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
