# Reconciliation report (synthetic data)

## Credit card vs QuickBooks ledger

| Metric | Value |
|---|---|
| Card lines | 306 |
| Auto-matched (no human touch) | 261 (85.3%) |
| Sent to review as possible/ambiguous match | 20 |
| Card charges with no ledger entry | 25 |
| Duplicate ledger entries flagged | 10 |
| Ledger lines with no card charge | 13 |
| Value of unrecorded charges | $11,211.36 |

## Order system vs QuickBooks invoices

| Finding | Count | Dollar impact |
|---|---|---|
| AMOUNT_MISMATCH | 10 | $130.81 |
| CANCELLED_INVOICED | 4 | $1,213.65 |
| DUPLICATE_INVOICE | 6 | $1,710.99 |
| MISSING_INVOICE | 14 | $3,931.69 |
| ORPHAN_INVOICE | 5 | $799.89 |

## Accuracy against planted defects (synthetic ground truth)

| Check | Precision | Recall |
|---|---|---|
| Auto-matches correct | 100.0% | 98.9% (auto + review) |
| Unrecorded charges found | 88.0% | 100.0% |
| Duplicate entries | 100.0% | 100.0% |
| Invoice: MISSING_INVOICE | 100.0% | 100.0% |
| Invoice: AMOUNT_MISMATCH | 100.0% | 100.0% |
| Invoice: DUPLICATE_INVOICE | 100.0% | 100.0% |
| Invoice: CANCELLED_INVOICED | 100.0% | 100.0% |
| Invoice: ORPHAN_INVOICE | 100.0% | 100.0% |

Category proposals correct: 100.0% overall; 100.0% on the 88% of rows with confidence >= 0.90 (those are the rows a reviewer can bulk-approve).

> These numbers come from generated data with planted defects. They show the engine behaves as designed; they are not a claim about performance on real company books.

## Review queue

| Kind | Pending |
|---|---|
| AMOUNT_MISMATCH | 10 |
| CANCELLED_INVOICED | 4 |
| DUPLICATE_ENTRY | 10 |
| DUPLICATE_INVOICE | 6 |
| LEDGER_ONLY | 13 |
| MISSING_ENTRY | 25 |
| MISSING_INVOICE | 14 |
| ORPHAN_INVOICE | 5 |
| POSSIBLE_MATCH | 20 |
