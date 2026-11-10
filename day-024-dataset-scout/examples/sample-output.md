# Dataset guide: customer support ticket intent classification
> Fine-tune a small model to classify customer support tickets by intent, English, permissive licence

## Recommendation
Start with [enzo-joseph/customer-support-tickets](https://huggingface.co/datasets/enzo-joseph/customer-support-tickets) using the `en` config (`train` split); it provides 22.6K English support tickets with pre-split training and test sets, clear intent categories (`type` column), and preprocessed text variations.

## Shortlist
| Dataset | Fit | Size | Licence | Downloads | Last updated |
| :--- | :--- | :--- | :--- | :--- | :--- |
| [enzo-joseph/customer-support-tickets](https://huggingface.co/datasets/enzo-joseph/customer-support-tickets) | Excellent for English intent classification (has explicit intent/type, subject, and body columns) | 123.5K total rows (22.6K English train) | not stated | 565 | 6 Jul, 2026 |
| [gorkemsevinc/customer_support_tickets](https://huggingface.co/datasets/gorkemsevinc/customer_support_tickets) | Good alternative for ticket classification | 8.5K rows | not stated | 2.9K | 21 Dec, 2023 |

## Best pick in detail
- **Dataset:** [enzo-joseph/customer-support-tickets](https://huggingface.co/datasets/enzo-joseph/customer-support-tickets)
- **Splits & Configurations:** The dataset provides multiple configs (`all`, `de`, `en`). For English, use config `en` which contains a `train` split (22.6K rows) and a `test` split (5.6K rows).
- **Columns:** 21 columns including `subject`, `body`, `answer`, `type` (used as the intent class), `queue`, `priority`, `language`, and preprocessed text fields (`text`, `text_lemmatized`, etc.).
- **Example Row:** A ticket with subject `"Update Project Management Software"`, body `"Request to update the software for enhanced compatibility with the IntelliJ IDEA toolset and improved performance and efficiency."`, intent `type` `"Change"`, queue `"Returns and Exchanges"`, and priority `"low"`.
- **Caveats:** The licence is not explicitly stated in the Hub metadata, so you should inspect the repository files or author details to verify commercial usability before deploying.

## Load it
```python
from datasets import load_dataset

dataset = load_dataset("enzo-joseph/customer-support-tickets", name="en", split="train")
print(dataset[0])
```

## Before you rely on it
- **Licence:** Not stated on the repository metadata; verify terms directly with the author before commercial use.
- **Label Quality:** Generated or synthetic ticket attributes may not mirror the noise, typos, and complex multi-intent phrasing of your organization's real customer support queue.
- **Bias:** Synthetic datasets can exhibit uniform distributions or artifacts across queues (`queue`) and intent types (`type`) that real-world support data rarely has.
- **Staleness:** Generated recently in 2026, but verify whether the domain terminology matches current software or service requirements.