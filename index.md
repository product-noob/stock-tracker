---
layout: default
title: US Markets Briefs
---

# US Markets Briefs

Daily pre-market, mid-session and close recaps for the US equity portfolio
(GOOGL, NVDA, AAPL, MSFT) and watchlist (META, TSM, ASML, AMZN).
New briefs appear here automatically after each daily sync.

## Weekly letters

{% assign letters = site.pages | where_exp: "p", "p.path contains 'us-markets/weekly/'" | sort: "path" | reverse %}
{% for l in letters %}
- [{{ l.name | remove: ".md" }}]({{ l.url | relative_url }})
{% endfor %}

## Daily briefs

{% assign briefs = site.pages | where_exp: "p", "p.path contains 'us-markets/briefs/'" | sort: "path" | reverse %}
{% assign last_date = "" %}
{% for b in briefs %}
{% assign d = b.name | slice: 0, 10 %}
{% assign kind = b.name | slice: 11, 99 | remove: ".md" %}
{% if d != last_date %}
{% unless forloop.first %}

{% endunless %}
### {{ d }}
{% assign last_date = d %}
{% endif %}
- [{{ kind }}]({{ b.url | relative_url }})
{% endfor %}

---

*Analysis only, not financial advice. Prices may be delayed ~15 minutes.*
