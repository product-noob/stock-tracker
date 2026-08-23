"""Tests for the M3 identity resolver (scripts/resolve_identity.py).

Run with:
    venv/bin/python -m pytest tests/test_resolve_identity.py -q
    (or: venv/bin/python -m unittest tests.test_resolve_identity -v)

Two layers:

* Offline pure-ladder tests against a hand-built :class:`Master` covering the
  fail-closed acceptance matrix from the module spec:

  - deterministic tiers fire in order (scrip code → cross-exchange anchor →
    document identifier → exact canonical name → curated alias);
  - exact alias hit resolves;
  - ambiguous multi-company match → review queue, never auto-linked;
  - no-match → review queue, never dropped or guessed;
  - non-whitelisted master hits are quarantined to review (a whitelist
    company can never be mis-linked through a lookalike);
  - NO fuzzy matching: prefix/substring names cannot publish an assignment.

* A DB-backed end-to-end test against the live ``stocktracker_db`` seeding
  fixture companies/aliases plus namespaced ``normalized_records`` rows, then
  asserting on ``identity_resolutions``, ``identity_assignments``, the
  ``normalized_records.company_id`` cache, and outbox events — including the
  one-transaction agreement invariant and re-run idempotency.

Like the other stage tests, fixture rows live in append-only tables and are
namespaced under a per-test uuid (``IDENTTST<uid>`` dedup scope) rather than
cleaned up. The run also resolves real dev-DB records — that is the
production path, and every assertion filters strictly on its own ids.
"""
import sys
import unittest
import uuid

ROOT = __file__.resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import resolve_identity as ri  # noqa: E402


def _database_url():
    try:
        return __import__("jobs").db_url_from_env()
    except Exception:  # noqa: BLE001 — DB absent → skip DB layer
        return None


# --------------------------------------------------------------------- #
# offline pure-ladder tests
# --------------------------------------------------------------------- #

def make_master(**overrides) -> ri.Master:
    base = dict(
        listings_by_code={},          # (exchange, code) -> {(company, listing)}
        listings_by_symbol={},        # (exchange, symbol) -> {(company, listing)}
        isin_to_companies={},         # isin -> {company}
        name_to_companies={},         # normalized name -> {company}
        alias_to_companies={},        # normalized alias -> {company}
        whitelist=frozenset(),
    )
    base.update(overrides)
    return ri.Master(**base)


def record(**overrides):
    base = dict(exchange="NSE", exchange_code_raw=None,
                exchange_symbol_raw=None, isin_raw=None,
                company_name_raw=None, title=None)
    base.update(overrides)
    return base


class TestNormalizeName(unittest.TestCase):
    def test_case_quotes_and_whitespace_collapsed(self):
        self.assertEqual(ri.normalize_name('"  State   Bank of India "'),
                         "state bank of india")
        self.assertEqual(ri.normalize_name(None), "")
        self.assertEqual(ri.normalize_name("RIL"), "ril")

    def test_no_stemming_or_punctuation_folding(self):
        """Exactness guard: 'L&T' must NOT equal 'l and t'."""
        self.assertNotEqual(ri.normalize_name("L&T"), "l and t")


class TestResolutionLadder(unittest.TestCase):
    WHITELIST = frozenset({1, 2, 3})

    def ladder(self, master, **rec):
        return ri.resolve_record(record(**rec), master)

    # -- tier A ----------------------------------------------------------

    def test_scrip_code_resolves_with_listing(self):
        m = make_master(
            listings_by_code={("BSE", "500325"): {(1, 11)}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, exchange="BSE", exchange_code_raw="500325")
        self.assertEqual(d.outcome, "resolved")
        self.assertEqual(d.tier, "A_scripcode")
        self.assertEqual((d.company_id, d.listing_id), (1, 11))
        self.assertFalse(d.needs_review)

    # -- tier B ----------------------------------------------------------

    def test_cross_exchange_symbol_anchor(self):
        m = make_master(
            listings_by_symbol={("BSE", "RELIANCE"): {(1, 12)}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, exchange="NSE", exchange_symbol_raw="Reliance")
        self.assertEqual(d.outcome, "resolved")
        self.assertEqual(d.tier, "B_cross_exchange")

    # -- tier C ----------------------------------------------------------

    def test_isin_beats_same_exchange_symbol(self):
        m = make_master(
            isin_to_companies={"INE002A01018": {1}},
            listings_by_symbol={("NSE", "RELIANCE"): {(2, 21)}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, isin_raw="INE002A01018",
                        exchange_symbol_raw="RELIANCE")
        self.assertEqual(d.tier, "C_document_identifier")
        self.assertEqual(d.company_id, 1)          # ISIN wins over symbol

    def test_own_exchange_symbol_resolves_at_symbol_confidence(self):
        m = make_master(
            listings_by_symbol={("NSE", "TCS"): {(3, 31)}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, exchange_symbol_raw="TCS")
        self.assertEqual(d.outcome, "resolved")
        self.assertEqual(d.tier, "C_document_identifier")
        self.assertAlmostEqual(float(d.confidence), ri.SYMBOL_CONFIDENCE)

    # -- tiers D / E -------------------------------------------------------

    def test_exact_canonical_name_resolves(self):
        m = make_master(
            name_to_companies={"state bank of india": {5}},
            whitelist=self.WHITELIST | {5})
        d = self.ladder(m, company_name_raw="State Bank of India")
        self.assertEqual(d.outcome, "resolved")
        self.assertEqual(d.tier, "D_exact_name")
        self.assertEqual(d.company_id, 5)

    def test_exact_alias_hit_resolves_fail_open_only_for_unique(self):
        m = make_master(
            alias_to_companies={"ril": {1}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, company_name_raw='" RIL "')
        self.assertEqual(d.outcome, "resolved")
        self.assertEqual(d.tier, "E_alias_name")
        self.assertEqual(d.company_id, 1)
        self.assertFalse(d.needs_review)

    # -- fail-closed matrix ------------------------------------------------

    def test_ambiguous_multi_company_match_goes_to_review(self):
        m = make_master(
            alias_to_companies={"shared tag": {1, 2}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, company_name_raw="Shared Tag")
        self.assertEqual(d.outcome, "unresolved")
        self.assertTrue(d.needs_review)
        self.assertIsNone(d.company_id)
        self.assertIn("ambiguous", d.reason)
        self.assertIn("1", d.reason) and self.assertIn("2", d.reason)

    def test_ambiguous_exact_names_go_to_review(self):
        m = make_master(
            name_to_companies={"twin industries limited": {1, 2, 3}},
            whitelist=self.WHITELIST)
        d = self.ladder(m, company_name_raw="Twin Industries Limited")
        self.assertEqual(d.outcome, "unresolved")
        self.assertTrue(d.needs_review)
        self.assertIsNone(d.company_id)

    def test_no_match_goes_to_review_never_dropped(self):
        d = self.ladder(make_master(whitelist=self.WHITELIST),
                        company_name_raw="Zzyzx Unknown Industries")
        self.assertEqual(d.outcome, "unresolved")
        self.assertTrue(d.needs_review)
        self.assertIsNone(d.company_id)
        self.assertEqual(d.confidence, 0.0)

    def test_non_whitelisted_hit_never_publishes(self):
        """Deterministic hit outside the whitelist quarantines to review."""
        m = make_master(
            listings_by_symbol={("NSE", "NOTWL"): {(99, 91)}},
            name_to_companies={"lookalike limited": {98}},
            alias_to_companies={"wl alias": {97}},
            isin_to_companies={"INE9999999999": {96}},
            whitelist=self.WHITELIST)     # none of 96..99 whitelisted
        cases = [
            self.ladder(m, exchange_symbol_raw="NOTWL"),
            self.ladder(m, company_name_raw="Lookalike Limited"),
            self.ladder(m, company_name_raw="Wl Alias"),
            self.ladder(m, isin_raw="INE9999999999"),
        ]
        for d in cases:
            self.assertEqual(d.outcome, "unresolved", d)
            self.assertTrue(d.needs_review, d)
            self.assertIsNone(d.company_id, d)
            self.assertIn("non-whitelisted", d.reason)

    def test_whitelist_company_never_mis_linked_via_prefix_or_substring(self):
        """No fuzzy tier: 'HDFC' must not auto-link HDFC Bank (alias
        'hdfc bank'); 'HDFC Ba' likewise. Both land in review instead."""
        m = make_master(
            name_to_companies={"hdfc bank limited": {2}},
            alias_to_companies={"hdfc bank": {2}, "hdfcbank": {2}},
            whitelist=self.WHITELIST | {2})
        for raw in ("HDFC", "HDFC Ba", "hdfcbank ltd", "The HDFC Bank"):
            d = self.ladder(m, company_name_raw=raw)
            self.assertEqual(d.outcome, "unresolved", raw)
            self.assertTrue(d.needs_review, raw)
            self.assertIsNone(d.company_id, raw)

    def test_below_threshold_confidence_cannot_resolve(self):
        """If any tier's score were ever lowered under the threshold, the
        candidate must still fail closed to review."""
        conf = dict(ri.TIER_CONFIDENCE, E_alias_name=0.5)
        with unittest.mock.patch.object(ri, "TIER_CONFIDENCE", conf):
            d = ri._decide("E_alias_name", {(1, None)})
        self.assertEqual(d.outcome, "unresolved")
        self.assertTrue(d.needs_review)


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
        cls.scope = f"identtest:{cls.uid}"

        # fixture companies: two whitelisted sharing an alias (ambiguity),
        # one deliberately NOT whitelisted (whitelist gate)
        cls.co_alpha = cls._fixture_company(f"Identity Test Alpha {cls.uid}")
        cls.co_beta = cls._fixture_company(f"Identity Test Beta {cls.uid}")
        cls.co_gamma = cls._fixture_company(
            f"Identity Test Gamma {cls.uid}", whitelist=False)
        shared = f"shared identity tag {cls.uid}"
        for cid in (cls.co_alpha, cls.co_beta):
            cls.conn.execute(
                """INSERT INTO core.company_aliases
                       (company_id, alias_value, normalized_value,
                        alias_kind, source)
                   VALUES (%s, %s, %s, 'name_variant', 'ident-test')""",
                (cid, shared, shared))
        cls.conn.commit()

        # real seeded companies used for positive paths
        cls.sbin = cls._company_id("State Bank of India")
        cls.reliance = cls._company_id("Reliance Industries Limited")
        cls.tcs = cls._company_id("Tata Consultancy Services Limited")
        cls.hdfcbank = cls._company_id("HDFC Bank Limited")

        cls.records = {
            "exact_name": cls._fixture_record(
                "exact", company_name_raw="State Bank of India"),
            "alias": cls._fixture_record(
                "alias", company_name_raw="RIL"),
            "symbol": cls._fixture_record(
                "sym", exchange_symbol_raw="TCS"),
            "scrip": cls._fixture_record(
                "scrip", exchange="BSE", exchange_code_raw="500112"),
            "ambiguous": cls._fixture_record(
                "ambig", company_name_raw=f"Shared Identity Tag {cls.uid}"),
            "no_match": cls._fixture_record(
                "nomatch",
                company_name_raw=f"Zzyzx Unknown Industries {cls.uid}"),
            "non_whitelisted": cls._fixture_record(
                "notwl",
                company_name_raw=f"Identity Test Gamma {cls.uid} Limited"),
            "fuzzy_trap": cls._fixture_record(
                "fuzzy", company_name_raw="HDFC"),
        }
        cls.conn.commit()

    @classmethod
    def _company_id(cls, canonical_name):
        return cls.conn.execute(
            "SELECT company_id FROM core.companies "
            "WHERE lower(canonical_name)=lower(%s)",
            (canonical_name,)).fetchone()[0]

    @classmethod
    def _fixture_company(cls, name, whitelist=True):
        cid = cls.conn.execute(
            "INSERT INTO core.companies (canonical_name) VALUES (%s) "
            "RETURNING company_id", (name,)).fetchone()[0]
        if whitelist:
            cls.conn.execute(
                "INSERT INTO core.whitelist_memberships (company_id, reason)"
                " VALUES (%s, 'ident resolver test fixture')", (cid,))
        return cid

    @classmethod
    def _fixture_record(cls, tag, *, exchange="NSE", company_name_raw=None,
                        exchange_symbol_raw=None, exchange_code_raw=None):
        return cls.conn.execute(
            """
            INSERT INTO ingestion.normalized_records
                (record_type, dedup_scope, dedup_key, exchange, external_id,
                 company_name_raw, exchange_symbol_raw, exchange_code_raw,
                 title)
            VALUES ('exchange_announcement', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING record_id
            """,
            (cls.scope, f"IDENTTST{tag}-{cls.uid}", exchange,
             f"IDENTTST{tag}{cls.uid}", company_name_raw,
             exchange_symbol_raw, exchange_code_raw, company_name_raw),
        ).fetchone()[0]

    # -- query helpers ---------------------------------------------------

    def resolution_for(self, record_id):
        return self.conn.execute(
            """
            SELECT outcome, tier, company_id, listing_id, confidence,
                   needs_review, reason, method_version
            FROM ingestion.identity_resolutions
            WHERE record_id=%s
            ORDER BY attempted_at DESC, resolution_id DESC LIMIT 1
            """,
            (record_id,)).fetchall()

    def assignment_company(self, record_id):
        row = self.conn.execute(
            "SELECT company_id FROM ingestion.identity_assignments "
            "WHERE record_id=%s", (record_id,)).fetchone()
        return row[0] if row else None

    def cached_company(self, record_id):
        return self.conn.execute(
            "SELECT company_id FROM ingestion.normalized_records "
            "WHERE record_id=%s", (record_id,)).fetchone()[0]

    def outbox_types(self, record_id):
        return {r[0] for r in self.conn.execute(
            "SELECT event_type FROM platform.outbox_events WHERE "
            "(event_key=%s OR event_key=%s)",
            (f"identity.resolved:{record_id}",
             f"identity.review_required:{record_id}")).fetchall()}

    # -- the acceptance matrix -------------------------------------------

    def test_end_to_end_resolution_and_fail_closed_review(self):
        conn, rec = self.conn, self.records

        stats = ri.run_resolve(conn, quiet=True)
        conn.commit()
        self.assertGreaterEqual(stats["records_in"], len(rec))
        self.assertEqual(stats["errors"], 0)

        # stage_runs audit row written with this module's version
        ver = conn.execute(
            "SELECT code_version FROM ingestion.stage_runs WHERE "
            "stage_run_id=%s", (stats["stage_run_id"],)).fetchone()[0]
        self.assertEqual(ver, ri.IDENT_VERSION)

        expected = {
            "exact_name": ("resolved", "D_exact_name", self.sbin),
            "alias": ("resolved", "E_alias_name", self.reliance),
            "symbol": ("resolved", "C_document_identifier", self.tcs),
            "scrip": ("resolved", "A_scripcode", self.sbin),
            "ambiguous": ("unresolved", "E_alias_name", None),
            "no_match": ("unresolved", "E_alias_name", None),
            "non_whitelisted": ("unresolved", "D_exact_name", None),
            "fuzzy_trap": ("unresolved", "E_alias_name", None),
        }
        for tag, record_id in rec.items():
            rows = self.resolution_for(record_id)
            self.assertEqual(len(rows), 1, tag)
            outcome, tier, company_id, listing_id, conf, review, reason, mv = \
                rows[0]
            exp_outcome, exp_tier, exp_company = expected[tag]
            self.assertEqual(outcome, exp_outcome, tag)
            self.assertEqual(tier, exp_tier, tag)
            self.assertEqual(company_id, exp_company, tag)
            self.assertEqual(mv, ri.IDENT_VERSION, tag)
            if exp_outcome == "resolved":
                self.assertFalse(review, tag)
                # three stores agree: resolution, assignment pointer, cache
                self.assertEqual(self.assignment_company(record_id),
                                 exp_company, tag)
                self.assertEqual(self.cached_company(record_id),
                                 exp_company, tag)
                if tag == "scrip":
                    self.assertIsNotNone(listing_id, tag)
                self.assertIn("identity.resolved", self.outbox_types(record_id),
                              tag)
            else:
                self.assertTrue(review, tag)
                self.assertIsNone(listing_id, tag)
                self.assertTrue(reason, tag)
                # fail-closed: nothing assigned, cache stays NULL
                self.assertIsNone(self.assignment_company(record_id), tag)
                self.assertIsNone(self.cached_company(record_id), tag)
                self.assertIn("identity.review_required",
                              self.outbox_types(record_id), tag)

        # explicit fail-closed spot checks
        amb = self.resolution_for(rec["ambiguous"])[0]
        self.assertIn("ambiguous", amb[6])
        nwl = self.resolution_for(rec["non_whitelisted"])[0]
        self.assertIn("non-whitelisted", nwl[6])
        fz = self.resolution_for(rec["fuzzy_trap"])[0]
        self.assertNotEqual(fz[2], self.hdfcbank)   # never mis-linked
        self.assertIsNone(fz[2])

        # ---- idempotency: rerunning attempts nothing already decided -----
        before = {rid: self.resolution_for(rid)[0]
                  for rid in rec.values()}
        stats2 = ri.run_resolve(conn, quiet=True)
        conn.commit()
        after = {rid: self.resolution_for(rid)[0]
                 for rid in rec.values()}
        self.assertEqual(before, after)
        self.assertLessEqual(stats2["records_in"],
                             stats["records_in"] - len(rec))

    def test_manual_supersede_repoints_assignment_and_cache(self):
        conn, record_id = self.conn, self.records["alias"]

        # ensure the record is currently resolved by the system path
        current = self.assignment_company(record_id)
        self.assertEqual(current, self.reliance)

        new_resolution = ri.apply_manual_resolution(
            conn, record_id, self.tcs, decided_by="ident-test-operator")
        conn.commit()

        history = self.conn.execute(
            """
            SELECT outcome, tier, company_id, supersedes_resolution_id,
                   decided_by
            FROM ingestion.identity_resolutions
            WHERE record_id=%s ORDER BY resolution_id
            """,
            (record_id,)).fetchall()
        self.assertGreaterEqual(len(history), 2)
        first, manual = history[0], history[-1]
        self.assertEqual(manual[0], "resolved")
        self.assertEqual(manual[1], "F_manual")
        self.assertEqual(manual[2], self.tcs)
        self.assertIsNotNone(manual[3])             # supersedes prior row
        self.assertEqual(manual[4], "ident-test-operator")
        # append-only: original system decision untouched
        self.assertEqual(first[1], "E_alias_name")

        # pointer + cache atomically repointed
        self.assertEqual(self.assignment_company(record_id), self.tcs)
        self.assertEqual(self.cached_company(record_id), self.tcs)
        self.assertEqual(new_resolution, manual[0] if manual[3] == first[0]
                         else new_resolution)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()


if __name__ == "__main__":
    unittest.main()
