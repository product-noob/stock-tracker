# NSE / BSE RSS Findings

**Test date:** 22 August 2026

> **Dated source snapshot:** endpoint availability/item counts below were observed on the
> test date and were not re-fetched during the 2026-08-23 review. The ingestion model is the
> approved requirement; no NSE/BSE RSS partition is currently DB-integrated. Current API
> status and reliability gaps live in the module OVERVIEWs and comprehensive review.

## Conclusion

Both NSE and BSE expose usable RSS/XML feeds for collating company announcements.

- NSE publishes category-specific feeds from `nsearchives.nseindia.com`.
- BSE publishes RSS feeds from the beta site at `beta.bseindia.com`.
- BSE's main announcements feed includes `<scripcode>`, making company matching easier.
- Both feeds generally link to PDF attachments. RSS supplies metadata; deeper structured extraction may require downloading and parsing the PDF.

The previous conclusion that BSE had no public RSS feed was incorrect. The relevant source is [BSE RSS Feeds](https://beta.bseindia.com/rss-feed.html).

## NSE

Official directory: [NSE RSS Feeds](https://www.nseindia.com/static/rss-feed)

Verified feeds:

```text
https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
https://nsearchives.nseindia.com/content/RSS/Annual_Reports.xml
https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml
https://nsearchives.nseindia.com/content/RSS/brsr.xml
https://nsearchives.nseindia.com/content/RSS/Circulars.xml
https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml
https://nsearchives.nseindia.com/content/RSS/Corporate_Governance.xml
https://nsearchives.nseindia.com/content/RSS/Daily_Buyback.xml
https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml
https://nsearchives.nseindia.com/content/RSS/InsiderTrading.xml
https://nsearchives.nseindia.com/content/RSS/Integrated_Filing_Financials.xml
https://nsearchives.nseindia.com/content/RSS/Investor_Complaints.xml
https://nsearchives.nseindia.com/content/RSS/Offer_Documents.xml
https://nsearchives.nseindia.com/content/RSS/Related_Party_Trans.xml
https://nsearchives.nseindia.com/content/RSS/Sast_ReasonForEncumbrance.xml
https://nsearchives.nseindia.com/content/RSS/Sast_Regulation29.xml
https://nsearchives.nseindia.com/content/RSS/Sast_Regulation31.xml
https://nsearchives.nseindia.com/content/RSS/Secretarial_Compliance.xml
https://nsearchives.nseindia.com/content/RSS/Share_Transfers.xml
https://nsearchives.nseindia.com/content/RSS/Shareholding_Pattern.xml
https://nsearchives.nseindia.com/content/RSS/Statement_Of_Deviation.xml
https://nsearchives.nseindia.com/content/RSS/Unitholding_Patterns.xml
https://nsearchives.nseindia.com/content/RSS/Voting_Results.xml
```

Typical NSE item:

```xml
<item>
  <title>Balaji Phosphates Limited</title>
  <link>https://nsearchives.nseindia.com/corporate/...pdf</link>
  <description>Announcement text |SUBJECT: Trading Window</description>
  <pubDate>22-Aug-2026 14:26:26</pubDate>
</item>
```

The main announcements feed returned valid RSS/XML and 280 items during testing. NSE fields are usually `title`, `link`, `description`, and `pubDate`. A clean NSE symbol, ISIN, or BSE code is not consistently included, so use a security-master mapping.

## BSE

Official directory: [BSE RSS Feeds](https://beta.bseindia.com/rss-feed.html)

Verified feeds:

```text
https://beta.bseindia.com/data/xml/announcements.xml
https://beta.bseindia.com/Data/XML/AnnualReportFeed.xml
https://beta.bseindia.com/Data/XML/BoardMeetingsFeed.xml
https://beta.bseindia.com/Data/XML/CorpActionFeed.xml
https://beta.bseindia.com/Data/XML/FinancialResultsFeed.xml
https://beta.bseindia.com/Data/XML/InsiderTradingFeed.xml
https://beta.bseindia.com/Data/XML/ShareholdingPattern_Feed.xml
https://beta.bseindia.com/Data/XML/VotingResultFeed.xml
https://beta.bseindia.com/data/xml/notices.xml
https://beta.bseindia.com/data/xml/sensexrss.xml
```

All ten URLs returned HTTP 200 during testing. Category feeds returned `application/rss+xml`; notices and Sensex feeds returned `text/xml` while containing RSS 2.0 XML.

Typical BSE item:

```xml
<item>
  <title>NTPC Green Energy Ltd (544289)</title>
  <link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/...pdf</link>
  <scripcode>544289</scripcode>
  <description>Announcement text</description>
  <pubDate>22-Aug-2026 14:23:53</pubDate>
</item>
```

BSE fields are usually `title`, `scripcode`, `link`, `description`, and `pubDate`. Multiple securities can point to the same attachment, particularly funds and other instruments.

## Recommended ingestion model (updated for schema v1.2)

```text
source partition (one feed URL / symbol / scrip / query)
  → fetch run + exact raw response artifact
  → idempotent raw items
  → canonical source records + all raw sightings
  → company / instrument / listing resolver
  → durable content-fetch job
  → completed attempt + SHA-256 content object
  → versioned text extraction
```

Canonical source-record fields include:

```text
record_type, dedup_scope, dedup_key, exchange?, external_id?,
company_name_raw, exchange_symbol_raw, exchange_code_raw, isin_raw,
title, subject, description, category_raw, content_url?, canonical_url?,
published_at?, first_seen_at, resolved_company_id?
```

Rules:

- Treat each feed URL or API company query as an independent source partition with its own checkpoint and overlapping poll window.
- Save exact XML/JSON response bytes before parsing; parsed JSONB is not the replay source of truth.
- Use a source-native ID as the item/canonical key where one exists. Otherwise use an adapter-documented stable tuple; do **not** deduplicate announcements by document URL because distinct records can share one PDF.
- Keep M:N links from canonical records to all raw feed sightings and from records to content objects.
- Deduplicate exact content by SHA-256 only after a completed fetch; persist failed/invalid attempts separately.
- Resolve company, instrument and exchange listing as distinct identities. ISIN identifies a security/share class, not the company or listing by itself.
- Poll frequently with overlap, log feed depth, and reconcile sampled dates against exchange pages/APIs. Cursor advancement alone does not prove completeness.

The executable model is documented in `docs/database-schema.md` and `db/schema.sql`.

Minimal parser:

```python
import feedparser

URLS = {
    "nse": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "bse": "https://beta.bseindia.com/data/xml/announcements.xml",
}

for exchange, url in URLS.items():
    for item in feedparser.parse(url).entries:
        print({
            "exchange": exchange,
            "company_name": item.get("title"),
            "bse_scripcode": item.get("scripcode"),
            "document_url": item.get("link"),
            "description": item.get("description"),
            "published_at": item.get("published"),
        })
```

## Limitations

- RSS metadata is not equivalent to fully structured filing data; important details may only exist in the PDF.
- Feed retention and item limits are not documented; slow polling may miss announcements.
- Do not rely on company-name matching alone.
- Handle UTF-8 BOM/encoding issues; a BSE response included a BOM during testing.
- Respect exchange terms, rate limits, caching, and reasonable polling intervals.
- Monitor the BSE beta hostname and paths for changes before production use.

## Sources

- [NSE RSS Feeds](https://www.nseindia.com/static/rss-feed)
- [BSE RSS Feeds](https://beta.bseindia.com/rss-feed.html)
- [BSE announcements RSS](https://beta.bseindia.com/data/xml/announcements.xml)
- [BSE annual reports RSS](https://beta.bseindia.com/Data/XML/AnnualReportFeed.xml)
- [BSE board meetings RSS](https://beta.bseindia.com/Data/XML/BoardMeetingsFeed.xml)
- [BSE corporate actions RSS](https://beta.bseindia.com/Data/XML/CorpActionFeed.xml)
- [BSE financial results RSS](https://beta.bseindia.com/Data/XML/FinancialResultsFeed.xml)
- [BSE insider trading RSS](https://beta.bseindia.com/Data/XML/InsiderTradingFeed.xml)
- [BSE shareholding RSS](https://beta.bseindia.com/Data/XML/ShareholdingPattern_Feed.xml)
- [BSE voting results RSS](https://beta.bseindia.com/Data/XML/VotingResultFeed.xml)
