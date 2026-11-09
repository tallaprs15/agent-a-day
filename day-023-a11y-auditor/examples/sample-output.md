<!-- Real output of: python -m a11y_auditor https://demo.playwright.dev/todomvc/ -->

## Summary
This audit evaluated the TodoMVC demo page (`https://demo.playwright.dev/todomvc/`) for accessibility against the specified WCAG Level A and AA criteria. The page has good headings and labeling for its primary input, but lacks landmark regions and skip links to bypass repeated content.

## Scorecard
| WCAG | Criterion | Result | Findings |
|---|---|---|---|
| 1.1.1 | Non-text Content (Level A) | Pass | - |
| 1.3.1 | Info and Relationships (Level A) | Pass | - |
| 2.1.1 | Keyboard (Level A) | Pass | - |
| 2.4.1 | Bypass Blocks (Level A) | Fail | A11Y-1 |
| 2.4.2 | Page Titled (Level A) | Pass | - |
| 2.4.4 | Link Purpose (In Context) (Level A) | Pass | - |
| 2.4.6 | Headings and Labels (Level AA) | Pass | - |
| 3.3.2 | Labels or Instructions (Level A) | Pass | - |
| 4.1.2 | Name, Role, Value (Level A) | Pass | - |

## Findings
### A11Y-1 · Missing Landmarks and Bypass Links
- **WCAG:** 2.4.1
- **Severity:** moderate
- **Where:** `https://demo.playwright.dev/todomvc/#/`
- **Evidence:** `- generic [ref=e1]:`
- **Fix:** Wrap main content in a `<main>` landmark element and consider adding a skip link to help keyboard users bypass repeated navigation or headers.

---
`— 7.1s · 5 model calls · 4/12 tool calls {browser_navigate: 1, browser_snapshot: 2, browser_press_key: 1} · 9,853 in / 449 out tokens` (no price configured for this model, so no USD estimate)
