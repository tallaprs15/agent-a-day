# Performance audit: https://web.dev
*(Note: CPU throttling at 4x and Fast 4G network throttling were applied for this audit).*

## Core Web Vitals
| Metric | Value | Rating |
|---|---|---|
| LCP | 1284 ms | Good |
| CLS | 0.01 | Good |
| INP | not measured | n/a |
| FCP | not measured | n/a |
| TTFB | 200 ms | Good |

## Diagnosis
The Largest Contentful Paint (LCP) was measured at 1,284 ms, which is classified as Good. According to the `LCPBreakdown` insight, the breakdown of the LCP time (1,284 ms) for the image resource `https://web.dev/static/images/home-blue_720.png` is as follows:
- Time to first byte: 200 ms (15.6% of total LCP time)
- Resource load delay: 87 ms (6.8% of total LCP time)
- Resource load duration: 16 ms (1.2% of total LCP time)
- Element render delay: 981 ms (76.4% of total LCP time)

The vast majority of the LCP time (981 ms) is spent in element render delay.

The Cumulative Layout Shift (CLS) was measured at 0.01 (Good). The `CLSCulprits` insight identified a layout shift cluster starting at 1,219 ms and ending at 2,496 ms with a score of 0.0094, caused by unsized images such as `https://web.dev/static/images/home-blue_720.png` and `https://web.dev/static/images/blog-header_480.png`.

The `RenderBlocking` insight found 3 render-blocking CSS requests with a total estimated savings of FCP 64 ms and LCP 0 ms. These include:
- `https://www.gstatic.com/devrel-devsite/prod/.../web/css/app.css` (duration: 153 ms)
- `https://fonts.googleapis.com/css2?family=Material+Icons...&display=block` (duration: 123 ms)
- `https://fonts.googleapis.com/css?family=Google+Sans...&display=swap` (duration: 97 ms)

## Recommendations
1. **Reduce Element Render Delay**: Address the element render delay, which accounted for 981 ms (76.4%) of the 1,284 ms total LCP time.
2. **Optimize Render-Blocking CSS Requests**: Address render-blocking stylesheets to improve initial rendering (estimated FCP savings of 64 ms), specifically:
   - `https://www.gstatic.com/devrel-devsite/prod/.../web/css/app.css` (total duration: 153 ms)
   - `https://fonts.googleapis.com/css2?family=Material+Icons...&display=block` (total duration: 123 ms)
   - `https://fonts.googleapis.com/css?family=Google+Sans...&display=swap` (total duration: 97 ms)
3. **Prevent Layout Shifts from Unsized Images**: Add explicit dimensions to images such as `https://web.dev/static/images/home-blue_720.png` and `https://web.dev/static/images/blog-header_480.png` to eliminate layout shifts within the shift cluster (score 0.0094).

---
`— 29.7s · 7 model calls · 6/10 tool calls {'new_page': 1, 'emulate': 1, 'performance_start_trace': 1, 'performance_analyze_insight': 3} · 36,384 in / 1,010 out tokens`
