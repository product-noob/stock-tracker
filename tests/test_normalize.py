"""Tests for the M2 normalizer (scripts/normalize.py).

Run with:
    venv/bin/python -m pytest tests/test_normalize.py -q
    (or: venv/bin/python -m unittest tests.test_normalize -v)

Two layers:

* Offline pure-helper tests: NSE/BSE field mapping, fail-closed shape gate,
  timestamp parsing (IST → UTC), fingerprint stability/change sensitivity,
  revision-key building.
* A DB-backed end-to-end test against the live ``stocktracker_db`` covering
  the TODO acceptance matrix:

  - happy path: raw item → normalized record + sighting link + outbox event;
  - duplicate input: re-normalizing is idempotent (no new record, link
    deduped);
  - duplicate sighting of the same native id via another partition links to
    the same canonical record;
  - version increment: changed upstream content appends ``@v2`` (append-only,
    original row untouched) and both sightings stay linked;
  - malformed input (missing native id / null payload) is quarantined in
    ``ingestion.stage_errors`` — never silently dropped, never guessed into a
    record.

Like the capture-adapter tests, these rows live in append-only tables, so
nothing is cleaned up: every fixture row is namespaced under a per-test uuid
(``TESTNORM<uid>`` partition keys) and left behind as audit evidence. Note
that ``run_normalize`` processes ALL pending items, including real dev-DB
items — that is by design (it is the production path) and every fixture
assertion below filters strictly on its own namespaced ids.
"""
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import normalize  # noqa: E402


def _database_url():
    try:
        return __import__("jobs").db_url_from_env()
    except Exception:  # noqa: BLE001 — DB absent → skip DB layer
        return None


# --------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------- #

def nse_payload(**overrides):
    payload = {
        "seq_id": 106000001,
        "symbol": "TESTNORM",
        "sm_name": "Test Norm Industries Limited",
        "sm_isin": "INE000TEST01",
        "attchmntText": "Company has informed the Exchange about Test",
        "desc": "General Updates",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/x.pdf",
        "an_dt": "20-Aug-2026 22:59:40",
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    if overrides.get("seq_id", True) is None:
        payload.pop("seq_id", None)
    return payload


def bse_payload(**overrides):
    payload = {
        "NEWSID": 108660001,
        "SCRIP_CD": 532999,
        "SLONGNAME": "Test Norm Industries Limited",
        "CATEGORYNAME": "Board Meeting",
        "HEADLINE": "Board Meeting Intimation",
        "ATTACHMENTNAME": "108660001.pdf",
        "NEWS_DT": "2026-08-20T18:30:00",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------- #

class TestNormalizeItem(unittest.TestCase):
    def test_nse_happy_path(self):
        norm = normalize.normalize_item("nse_api", nse_payload())
        self.assertEqual(norm["external_id"], "106000001")
        self.assertEqual(norm["record_type"], "exchange_announcement")
        self.assertEqual(norm["exchange"], "NSE")
        self.assertEqual(norm["company_name_raw"],
                         "Test Norm Industries Limited")
        self.assertEqual(norm["exchange_symbol_raw"], "TESTNORM")
        self.assertEqual(norm["isin_raw"], "INE000TEST01")
        self.assertEqual(norm["subject"], "General Updates")
        self.assertTrue(norm["description"].startswith("Company has informed"))
        self.assertEqual(
            norm["content_url"],
            "https://nsearchives.nseindia.com/corporate/x.pdf")
        self.assertEqual(
            norm["published_at"],
            datetime(2026, 8, 20, 17, 29, 40, tzinfo=timezone.utc))

    def test_bse_happy_path(self):
        norm = normalize.normalize_item("bse_api", bse_payload())
        self.assertEqual(norm["external_id"], "108660001")
        self.assertEqual(norm["record_type"], "exchange_announcement")
        self.assertEqual(norm["exchange"], "BSE")
        self.assertEqual(norm["exchange_code_raw"], "532999")
        self.assertIsNone(norm["exchange_symbol_raw"])
        self.assertIsNone(norm["isin_raw"])  # BSE API carries no ISIN
        self.assertEqual(norm["subject"], "Board Meeting")
        self.assertEqual(norm["category_raw"], "Board Meeting")
        self.assertEqual(norm["description"], "Board Meeting Intimation")
        self.assertEqual(
            norm["content_url"],
            "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
            "108660001.pdf")
        self.assertEqual(
            norm["published_at"],
            datetime(2026, 8, 20, 13, 0, 0, tzinfo=timezone.utc))

    def test_bse_no_attachment_gives_null_url(self):
        norm = normalize.normalize_item("bse_api",
                                        bse_payload(ATTACHMENTNAME=" "))
        self.assertIsNone(norm["content_url"])

    def test_missing_native_ids_fail_closed(self):
        with self.assertRaises(normalize.NormalizationError):
            normalize.normalize_item("nse_api", nse_payload(seq_id=None))
        with self.assertRaises(normalize.NormalizationError):
            normalize.normalize_item("bse_api", bse_payload(NEWSID=""))

    def test_non_object_payload_fails_closed(self):
        for bad in ([1, 2], "string", 42, None):
            with self.assertRaises(normalize.NormalizationError):
                normalize.normalize_item("nse_api", bad)

    def test_unknown_source_fails_closed(self):
        with self.assertRaises(normalize.NormalizationError):
            normalize.normalize_item("google_news", {"id": 1})

    def test_unparseable_timestamp_stays_none_not_error(self):
        """M2 card: missing/unparseable publication timestamps permitted."""
        norm = normalize.normalize_item("nse_api",
                                        nse_payload(an_dt="not-a-date"))
        self.assertIsNone(norm["published_at"])

    def test_timestamp_variants(self):
        self.assertEqual(
            normalize.parse_news_dt("2026-08-20T18:30:00.123"),
            datetime(2026, 8, 20, 13, 0, 0, 123000, tzinfo=timezone.utc))
        self.assertEqual(
            normalize.parse_news_dt("20-Aug-2026 18:30:00"),
            datetime(2026, 8, 20, 13, 0, 0, tzinfo=timezone.utc))
        self.assertIsNone(normalize.parse_news_dt("junk"))
        self.assertIsNone(normalize.parse_an_dt(None))


class TestFingerprintAndKeys(unittest.TestCase):
    def test_fingerprint_stable_across_repeats(self):
        a = normalize.normalize_item("nse_api", nse_payload())
        b = normalize.normalize_item("nse_api", nse_payload())
        self.assertEqual(normalize.fingerprint(a), normalize.fingerprint(b))

    def test_fingerprint_detects_content_change(self):
        a = normalize.normalize_item("nse_api", nse_payload())
        b = normalize.normalize_item(
            "nse_api",
            nse_payload(attchmntText="CORRECTED text from the exchange"))
        self.assertNotEqual(normalize.fingerprint(a),
                            normalize.fingerprint(b))

    def test_fingerprint_ignores_external_id(self):
        """Identity lives in dedup_key; fingerprints compare content only."""
        a = normalize.normalize_item("nse_api", nse_payload(seq_id=1))
        b = normalize.normalize_item("nse_api", nse_payload(seq_id=2))
        self.assertEqual(a["external_id"], "1")
        self.assertEqual(b["external_id"], "2")
        self.assertEqual(normalize.fingerprint(a), normalize.fingerprint(b))

    def test_record_matches_compares_stored_row(self):
        norm = normalize.normalize_item("nse_api", nse_payload())
        stored = dict(norm)
        self.assertTrue(normalize.record_matches(stored, norm))
        changed = dict(norm, description="edited upstream")
        self.assertFalse(normalize.record_matches(changed, norm))

    def test_revision_keys(self):
        self.assertEqual(normalize.revision_key("123", 1), "123@v1")
        self.assertEqual(normalize.revision_key("123", 12), "123@v12")


# --------------------------------------------------------------------- #
# DB-backed end-to-end (live stocktracker_db)
# --------------------------------------------------------------------- #

@unittest.skipUnless(_database_url(), "stocktracker_db not reachable")
class TestDbBacked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jobs
        cls.jobs = jobs
        cls.conn = jobs.connect()
        cls.uid = uuid.uuid4().hex[:10]
        cls._parts = {}
        cls._setup_partition("nse_api", f"symbol:TESTNORM{cls.uid}")
        cls._setup_partition("nse_api", f"symbol:TESTNRM2{cls.uid}")

    @classmethod
    def _setup_partition(cls, source, key):
        row = cls.conn.execute(
            """
            INSERT INTO ingestion.source_partitions
                (source, partition_key, description, config)
            VALUES (%s, %s, 'normalize test fixture', '{}'::jsonb)
            RETURNING partition_id
            """,
            (source, key)).fetchone()
        cls._parts.setdefault(source, []).append(row[0])
        cls.conn.commit()

    def _seq(self, tag: str) -> int:
        """A unique numeric native id namespaced by this test's uuid."""
        digits = "".join(ch for ch in f"{tag}{self.uid}" if ch.isdigit()
                         )[:7].ljust(7, "7")
        return int(digits)

    def insert_raw_item(self, part_pos, idem_suffix, external_id, payload,
                        published_at=None):
        part_id = self._parts["nse_api"][part_pos]
        run_key = f"normtest:{self.uid}:{part_pos}:{idem_suffix}"
        run = self.conn.execute(
            """
            INSERT INTO ingestion.fetch_runs
                (partition_id, idempotency_key, status, finished_at)
            VALUES (%s, %s, 'ok', now())
            RETURNING fetch_run_id
            """,
            (part_id, run_key)).fetchone()[0]
        art_key = f"{run_key}:art"
        art = self.conn.execute(
            """
            INSERT INTO ingestion.raw_artifacts
                (fetch_run_id, idempotency_key, artifact_kind, sha256,
                 storage_path, byte_size)
            VALUES (%s, %s, 'api_response', %s, '/tmp/normtest', 4)
            RETURNING artifact_id
            """,
            (run, art_key, ("b" + idem_suffix)[:2] * 32)).fetchone()[0]
        item_id = self.conn.execute(
            """
            INSERT INTO ingestion.raw_feed_items
                (partition_id, first_seen_run_id, artifact_id,
                 idempotency_key, external_id, published_at, title,
                 description, content_url, raw_payload, raw_fragment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING item_id
            """,
            (part_id, run, art, f"nse_ann:{idem_suffix}", external_id,
             published_at,
             (payload or {}).get("sm_name"),
             (payload or {}).get("attchmntText"),
             payload.get("attchmntFile") if payload else None,
             json.dumps(payload) if payload is not None else None,
             None if payload is not None else json.dumps(
                 {"note": "unparsed fragment fixture"}))).fetchone()[0]
        self.conn.commit()
        return item_id

    # -- query helpers --------------------------------------------------

    def records_for(self, base):
        return self.conn.execute(
            """
            SELECT record_id, dedup_key FROM ingestion.normalized_records
            WHERE dedup_scope='nse_api'
              AND dedup_key ~ ('^' || %s || '@v[0-9]+$')
            ORDER BY dedup_key
            """,
            (base,)).fetchall()

    def links_for(self, record_id):
        return [r[0] for r in self.conn.execute(
            "SELECT item_id FROM ingestion.record_raw_items "
            "WHERE record_id=%s ORDER BY item_id",
            (record_id,)).fetchall()]

    def outbox_count(self, record_id):
        return self.conn.execute(
            "SELECT count(*) FROM platform.outbox_events "
            "WHERE event_key=%s",
            (f"normalized_record.created:{record_id}",)).fetchone()[0]

    def quarantined_for(self, *item_ids):
        rows = self.conn.execute(
            """
            SELECT entity_id FROM ingestion.stage_errors
            WHERE error_code='UNNORMALIZABLE_SHAPE'
              AND entity_type='raw_feed_item'
              AND entity_id::bigint = ANY(%s)
            """,
            ([int(i) for i in item_ids],)).fetchall()
        return {r[0] for r in rows}

    # -- the acceptance matrix ------------------------------------------

    def test_lifecycle_happy_duplicate_version_quarantine(self):
        seq = 910000000 + self._seq("a") % 100000000
        base = str(seq)

        # ---- happy path -------------------------------------------------
        item1 = self.insert_raw_item(
            0, f"{base}-first", base, nse_payload(seq_id=seq),
            published_at=datetime(2026, 8, 20, 17, 29, 40,
                                  tzinfo=timezone.utc))
        stats = normalize.run_normalize(self.conn, quiet=True)
        self.conn.commit()

        recs = self.records_for(base)
        self.assertEqual([r[1] for r in recs], [f"{base}@v1"])
        v1 = recs[0][0]
        self.assertIn(item1, self.links_for(v1))
        self.assertEqual(self.outbox_count(v1), 1)

        srow = self.conn.execute(
            "SELECT code_version FROM ingestion.stage_runs "
            "WHERE stage_run_id=%s",
            (stats["stage_run_id"],)).fetchone()
        self.assertEqual(srow[0], normalize.NORM_VERSION)

        row = self.conn.execute(
            """
            SELECT record_type, exchange, external_id, company_name_raw,
                   subject, description, published_at, company_id
            FROM ingestion.normalized_records WHERE record_id=%s
            """,
            (v1,)).fetchone()
        self.assertEqual(row[0], "exchange_announcement")
        self.assertEqual(row[1], "NSE")
        self.assertEqual(row[2], base)
        self.assertEqual(row[3], "Test Norm Industries Limited")
        self.assertIsNone(row[7])  # unresolved — attribution is M3's job

        # ---- duplicate input: fully idempotent --------------------------
        recs_before = recs
        normalize.run_normalize(self.conn, quiet=True)
        self.conn.commit()
        self.assertEqual(self.records_for(base), recs_before)
        # direct double-link attempt is a no-op too
        self.assertFalse(normalize.link_sighting(self.conn, v1, item1))
        self.conn.commit()

        # ---- duplicate sighting via another partition -------------------
        item2 = self.insert_raw_item(
            1, f"{base}-second", base, nse_payload(seq_id=seq),
            published_at=datetime(2026, 8, 20, 17, 29, 40,
                                  tzinfo=timezone.utc))
        normalize.run_normalize(self.conn, quiet=True)
        self.conn.commit()
        self.assertEqual(self.records_for(base), recs_before)   # still v1 only
        linked_v1 = self.links_for(v1)
        self.assertIn(item1, linked_v1)
        self.assertIn(item2, linked_v1)

        # ---- version increment after upstream source change -------------
        item3 = self.insert_raw_item(
            0, f"{base}-corrected", base,
            nse_payload(seq_id=seq,
                        attchmntText="CORRECTED disclosure text v2",
                        desc="Credit Rating"),
            published_at=datetime(2026, 8, 21, 9, 0, 0,
                                  tzinfo=timezone.utc))
        normalize.run_normalize(self.conn, quiet=True)
        self.conn.commit()

        recs = self.records_for(base)
        self.assertEqual([r[1] for r in recs],
                         [f"{base}@v1", f"{base}@v2"])
        v2 = recs[1][0]
        self.assertIn(item3, self.links_for(v2))     # new sighting on v2
        # append-only: v1 row untouched, its links intact
        self.assertEqual(set(self.links_for(v1)), {item1, item2})
        self.assertEqual(self.outbox_count(v2), 1)
        stored = self.conn.execute(
            "SELECT subject, description FROM ingestion.normalized_records "
            "WHERE record_id=%s", (v2,)).fetchone()
        self.assertEqual(stored[0], "Credit Rating")
        self.assertEqual(stored[1], "CORRECTED disclosure text v2")

        # unchanged re-sighting of the corrected payload stays on v2
        item4 = self.insert_raw_item(
            1, f"{base}-again", base,
            nse_payload(seq_id=seq,
                        attchmntText="CORRECTED disclosure text v2",
                        desc="Credit Rating"),
            published_at=datetime(2026, 8, 21, 9, 0, 0,
                                  tzinfo=timezone.utc))
        normalize.run_normalize(self.conn, quiet=True)
        self.conn.commit()
        self.assertEqual(len(self.records_for(base)), 2)       # no v3
        self.assertIn(item4, self.links_for(v2))

        # ---- malformed input: quarantined fail-closed --------------------
        bad = 920000000 + self._seq("b") % 100000000
        bad_base = str(bad)
        item_bad = self.insert_raw_item(
            0, f"{bad}-noshape", bad_base,
            {"symbol": "TESTNORM"})          # missing seq_id
        item_null = self.insert_raw_item(
            0, f"{bad}-nullpay", bad_base, None)   # fragment-only, no payload
        self.conn.commit()

        stats_bad = normalize.run_normalize(self.conn, quiet=True)
        self.conn.commit()
        self.assertGreaterEqual(stats_bad["errors"], 2)   # batch completed
        self.assertEqual(self.records_for(bad_base), [])   # never guessed
        quarantined = self.quarantined_for(item_bad, item_null)
        self.assertIn(str(item_bad), quarantined)
        self.assertIn(str(item_null), quarantined)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()


if __name__ == "__main__":
    unittest.main()
