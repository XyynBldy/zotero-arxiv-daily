"""Regression checks for Online First, metadata lag, recovery and visible dates."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("digest_freshness", ROOT / "scripts/journal_digest.py")
jd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jd)
TODAY = date(2026, 11, 16)


def record(suffix, online=None, created=None, published=None, indexed=None):
    item = {"DOI": "10.1234/" + suffix, "title": [suffix]}
    for key, value in (("published-online", online), ("created", created),
                       ("published", published), ("indexed", indexed)):
        if value:
            item[key] = {"date-parts": [value]}
    return jd.make_crossref_paper(item, "J")


class FreshnessTests(unittest.TestCase):
    cfg = {"lookback_days": 7, "journals": [{"name": "J", "issn": "1365-1609"}]}

    def setUp(self):
        from tests.date_verification_helpers import offline_verify
        stub = patch.object(jd.PublisherDateVerifier, 'verify', offline_verify)
        stub.start()
        self.addCleanup(stub.stop)

    def select(self, papers, state=None, rss=None, status=None):
        cfg = dict(self.cfg)
        if rss is not None:
            cfg["journals"] = [dict(cfg["journals"][0], rss="https://example.org/feed")]
        with patch.object(jd, "fetch_crossref", return_value=papers), \
                patch.object(jd, "fetch_rss", return_value=rss or []):
            groups, keys, _ = jd.select_new(cfg, state or {"seen": {}}, None, TODAY,
                                           source_status=status)
        return groups, keys

    def test_online_date_controls_order_not_registration_date(self):
        earlier = record("earlier", [2026, 11, 14], [2026, 11, 16])
        latest = record("latest", [2026, 11, 16], [2026, 10, 1])
        status = {}
        groups, _ = self.select([earlier, latest], status=status)
        self.assertEqual([p["title"] for p in groups["J"]], ["latest", "earlier"])
        self.assertTrue(all(p["freshness"] == "recent_online" for p in groups["J"]))
        self.assertEqual(status["J"], "2026-11-16")

    def test_late_deposit_is_kept_as_backfill_with_original_online_date(self):
        paper = record("late", [2026, 11, 1], [2026, 11, 16])
        groups, _ = self.select([paper])
        self.assertEqual(groups["J"][0]["freshness"], "late_record")
        self.assertEqual(groups["J"][0]["online_date"], date(2026, 11, 1))

    def test_future_issue_date_is_unverified_and_never_recent_online(self):
        paper = record("issue", created=[2026, 11, 16], published=[2027, 1, 1])
        groups, _ = self.select([paper])
        self.assertEqual(groups["J"], [])
        text = jd.render_markdown(groups, TODAY, 7)
        self.assertNotIn("2027-01-01", text)
        self.assertNotIn("### 近期在线发表", text)

    def test_future_online_date_is_not_sent_or_marked_seen(self):
        paper = record("future", [2026, 11, 17], [2026, 11, 16])
        groups, keys = self.select([paper])
        self.assertEqual(groups["J"], [])
        self.assertEqual(keys, set())

    def test_partial_online_date_is_not_invented_as_first_day_of_month(self):
        paper = record("partial", [2026, 11], [2026, 11, 16])
        groups, _ = self.select([paper])
        self.assertIsNone(paper["online_date"])
        self.assertEqual(groups["J"], [])

    def test_seven_day_window_includes_today_and_six_previous_days(self):
        groups, _ = self.select([record("included", [2026, 11, 10]),
                                 record("excluded", [2026, 11, 9])])
        self.assertEqual([p["title"] for p in groups["J"]], ["included"])

    def test_rss_update_cannot_replace_old_crossref_online_date(self):
        crossref = record("old", [2026, 10, 1], [2026, 10, 1])
        rss = dict(crossref, source="RSS", online_date=None, pub_date=TODAY,
                   rss_date=TODAY, date_kind="RSS 更新时间")
        groups, _ = self.select([crossref], rss=[rss])
        self.assertEqual(len(groups["J"]), 1)
        self.assertEqual(groups["J"][0]["freshness"], "late_record")
        self.assertEqual(groups["J"][0]["online_date"], date(2026, 10, 1))

    def test_official_rss_explicit_available_online_is_used_but_issue_month_is_not(self):
        class Response:
            content = b'''<rss><channel>
              <item><title>Online paper</title><description>&lt;p&gt;Publication date: Available online 16 November 2026&lt;/p&gt;</description></item>
              <item><title>Future issue</title><description>&lt;p&gt;Publication date: January 2027&lt;/p&gt;</description></item>
            </channel></rss>'''
            def raise_for_status(self): pass
        class Session:
            def get(self, *args, **kwargs): return Response()
        official = jd.fetch_rss(Session(), "https://rss.sciencedirect.com/publication/science/13651609", "J", 5)
        self.assertEqual(len(official), 1)
        self.assertEqual(official[0]["online_date"], TODAY)
        self.assertIn("Available online", official[0]["online_date_source"])
        self.assertEqual(jd.fetch_rss(Session(), "https://example.org/rss", "J", 5), [])

    def test_rss_without_doi_can_supply_online_date_for_a_unique_matching_title(self):
        crossref = record("same-title", created=[2026, 11, 16], published=[2027, 1, 1])
        rss = dict(crossref, doi="", source="RSS", online_date=TODAY,
                   online_date_source="ScienceDirect 官方 RSS Available online",
                   pub_date=TODAY, rss_date=TODAY, date_kind="在线发表（官方 RSS）")
        groups, _ = self.select([crossref], rss=[rss])
        self.assertEqual(len(groups["J"]), 1)
        self.assertEqual(groups["J"][0]["freshness"], "recent_online")
        self.assertEqual(groups["J"][0]["doi"], "10.1234/same-title")

    def test_conflicting_online_dates_are_flagged_instead_of_silently_choosing_latest(self):
        crossref = record("conflict", [2026, 11, 15], [2026, 11, 16])
        rss = dict(crossref, source="RSS", online_date=TODAY, pub_date=TODAY,
                   rss_date=TODAY, online_date_source="ScienceDirect 官方 RSS Available online")
        cfg = dict(self.cfg, journals=[dict(self.cfg["journals"][0], rss="https://example.org/feed")])
        notes = []
        with patch.object(jd, "fetch_crossref", return_value=[crossref]), patch.object(jd, "fetch_rss", return_value=[rss]):
            groups, _, _ = jd.select_new(cfg, {"seen": {}}, None, TODAY, notes)
        self.assertEqual(groups["J"], [])
        self.assertIn("冲突", notes[0])

    def test_future_rss_update_is_not_reported_as_recent(self):
        paper = dict(record("rss"), source="RSS", pub_date=date(2026, 11, 17),
                     rss_date=date(2026, 11, 17), date_kind="RSS 更新时间")
        groups, _ = self.select([], rss=[paper])
        self.assertEqual(groups["J"], [])

    def test_recovery_includes_indexed_record_outside_recent_publication_window(self):
        state = {"seen": {}, "last_successful_fetch": "2026-11-01"}
        paper = record("recovery", [2026, 10, 1], indexed=[2026, 11, 5])
        groups, _ = self.select([paper], state=state)
        self.assertEqual(groups["J"][0]["freshness"], "late_record")

    def test_previously_sent_advance_record_is_announced_when_it_goes_online(self):
        paper = record("advance", [2026, 11, 16], [2026, 11, 12])
        state = {"seen": {"doi:10.1234/advance": "2026-11-12"}}
        groups, _ = self.select([paper], state=state)
        self.assertEqual(len(groups["J"]), 1)
        state["seen"]["doi:10.1234/advance"] = "2026-11-16"
        groups, _ = self.select([paper], state=state)
        self.assertEqual(groups["J"], [])

    def test_future_watermark_fails_instead_of_silently_skipping_a_window(self):
        with self.assertRaisesRegex(ValueError, "future"):
            self.select([], state={"seen": {}, "last_successful_fetch": "2026-11-17"})

    def test_date_time_only_index_and_timezone(self):
        value, exact = jd.crossref_date({"date-time": "2026-11-16T01:00:00+08:00"})
        self.assertEqual(value, date(2026, 11, 15))
        self.assertTrue(exact)
        self.assertEqual(jd.crossref_date({"date-parts": "bad"}), (None, False))

    def test_index_query_uses_saved_progress_and_does_not_truncate_at_first_page(self):
        class Response:
            def __init__(self, items, cursor=None):
                self.message = {"items": items, "next-cursor": cursor}
            def raise_for_status(self): pass
            def json(self): return {"message": self.message}
        raw = lambda suffix: {"DOI": "10.1234/" + suffix, "title": [suffix]}
        responses = [Response([raw("a"), raw("b")], "next"), Response([raw("c")]),
                     Response([]), Response([])]
        class Session:
            headers = {}
            calls = []
            def get(self, url, **kwargs):
                self.calls.append(dict(kwargs["params"]))
                return responses.pop(0)
        session = Session()
        with patch.object(jd.time, "sleep"):
            papers = jd.fetch_crossref(session, {"crossref": {"rows_per_page": 2}},
                                       "1365-1609", "J", date(2026, 11, 10), TODAY,
                                       date(2026, 10, 31))
        self.assertEqual(len(papers), 3)
        self.assertEqual(session.calls[1]["cursor"], "next")
        self.assertIn("from-index-date:2026-10-31", session.calls[-1]["filter"])
        self.assertIn("until-index-date:2026-11-16", session.calls[-1]["filter"])

    def run_main(self, papers, mail_error=None, rss_error=False):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        cfg = root / "config.yaml"
        cfg.write_text("lookback_days: 7\njournals:\n  - name: J\n    issn: '1365-1609'\n" +
                       ("    rss: https://example.org/feed\n" if rss_error else ""), encoding="utf8")
        state = root / "state.json"
        before = {"schema": 1, "seen": {}, "last_successful_fetch": "2026-11-01"}
        state.write_text(json.dumps(before), encoding="utf8")
        argv = ["--config", str(cfg), "--state", str(state), "--report-dir", str(root / "reports"),
                "--date", "2026-11-16"]
        with patch.object(jd, "fetch_crossref", return_value=papers), \
                patch.object(jd, "fetch_rss", side_effect=requests.HTTPError("403") if rss_error else None), \
                patch.object(jd, "send_email", side_effect=mail_error) as send, \
                patch.dict(os.environ, {"DRY_RUN": "false", "GITHUB_STEP_SUMMARY": ""}):
            if mail_error:
                with self.assertRaisesRegex(RuntimeError, "SMTP unavailable"):
                    jd.main(argv)
            else:
                self.assertEqual(jd.main(argv), 0)
        return json.loads(state.read_text(encoding="utf8")), before, send

    def test_empty_successful_scan_saves_progress_without_email(self):
        after, _, mail = self.run_main([])
        self.assertEqual(after["last_successful_fetch"], "2026-11-16")
        mail.assert_not_called()

    def test_smtp_failure_preserves_seen_records_and_fetch_progress(self):
        after, before, _ = self.run_main([record("fresh", [2026, 11, 16])],
                                        RuntimeError("SMTP unavailable"))
        self.assertEqual(after['seen'], before['seen'])
        self.assertEqual(after['last_successful_fetch'], before['last_successful_fetch'])
        self.assertIn('10.1234/fresh', after['pending_date_checks'])

    def test_zero_papers_with_failed_rss_sends_warning_and_preserves_progress(self):
        after, before, mail = self.run_main([], rss_error=True)
        mail.assert_called_once()
        self.assertIn("数据源异常", mail.call_args.args[0])
        self.assertEqual(after['seen'], before['seen'])
        self.assertEqual(after['last_successful_fetch'], before['last_successful_fetch'])

    def test_reports_separate_latest_backfill_and_unverified_records(self):
        groups, _ = self.select([record("latest", [2026, 11, 16]),
                                 record("late", [2026, 10, 1], [2026, 11, 16]),
                                 record("unknown", created=[2026, 11, 16])])
        for render in (jd.render_markdown, jd.render_html):
            report = render(groups, TODAY, 7, source_status={"J": "2026-11-16"})
            self.assertLess(report.index("近期在线发表（日期已核实）"), report.index("补录 / 元数据更新"))
            self.assertNotIn("新发现记录（在线发表日期待核实）", report)
            self.assertNotIn("**unknown**", report)
            self.assertIn("本次检索可核实的最新在线发表日期：2026-11-16", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
