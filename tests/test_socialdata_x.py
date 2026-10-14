"""Offline regressions: exercise the real CLI, mock only the HTTP transport."""
import contextlib
import csv
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("socialdata_x", ROOT / "x-research" / "socialdata_x.py")
x = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x)


@contextlib.contextmanager
def temporary_folder():
    # Keep test artifacts in a known workspace, including restricted desktop sandboxes.
    with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
        if Path(folder).resolve().parent != ROOT.parent.resolve():
            raise AssertionError("Temporary directory escaped test workspace")
        yield folder


def tweet(n, text="Example &amp; text", **extra):
    return {"id_str": str(1000000000000000000 + n), "full_text": text,
            "tweet_created_at": "2026-10-14T00:00:00Z", "favorite_count": 0,
            "user": {"screen_name": "example", "name": "Example"}, "entities": {}, **extra}


class Response:
    def __init__(self, data):
        self.payload = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class CliTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.calls = []
        self.pages = {}
        self.handler = self.respond
        self.stack.enter_context(patch.dict(os.environ, {"SOCIALDATA_API_KEY": "offline-test-placeholder"}))
        self.stack.enter_context(patch.object(x, "BASE", "https://api.socialdata.tools"))
        self.stack.enter_context(patch.object(x.time, "sleep"))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Live network forbidden")))
        self.stack.enter_context(patch.object(x.urllib.request, "urlopen", side_effect=self.request))

    def request(self, req, timeout):
        self.calls.append(req)
        return Response(self.handler(req))

    def respond(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path
        body = json.loads(req.data) if req.data else None
        if path == "/twitter/users-by-usernames":
            return [{"id_str": str(i + 12345), "screen_name": h} for i, h in enumerate(body["usernames"])]
        if path == "/twitter/tweets-by-ids":
            return [tweet(int(s) - 1000000000000000000) for s in body["ids"]]
        if path.startswith("/v2/twitter/article/"):
            return tweet(int(path.rsplit("/", 1)[1]) - 1000000000000000000,
                         markdown="Example full text", markdown_source="tweet_text")
        if path.startswith("/twitter/tweets/") and path.count("/") == 3:
            return tweet(int(path.rsplit("/", 1)[1]) - 1000000000000000000)
        if path.startswith("/twitter/user/") and path.count("/") == 3:
            return {"id_str": "12345", "screen_name": path.rsplit("/", 1)[1]}
        query = urllib.parse.parse_qs(url.query).get("query", [""])[0]
        key = (path, query)
        page = self.pages.get(key, 0)
        self.pages[key] = page + 1
        return {"tweets": [tweet(len(self.calls) * 1000 + i) for i in range(20)], "next_cursor": str(page + 1)}

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = x.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_whole_page_accounting(self):
        code, out, err = self.run_cli(["search", "AI", "--format", "json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["meta"]["count"], 50)
        self.assertEqual(data["meta"]["estimated_cost_usd"], .012)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(data["meta"]["complete"])

    def test_aggregate_budgets_rejected_before_http(self):
        ids = [str(1000000000000000000 + i) for i in range(1001)]
        cases = [
            ["user", "alice", "bob", "--per-account", "--max", "1000"],
            ["user", *["u" + str(i) for i in range(21)], "--max", "1000"],
            ["tweet", ids[0], "--thread", "--comments", "1000", "--quotes", "1000", "--max", "1000"],
            ["tweet", *ids], ["profile", *["u" + str(i) for i in range(1001)]],
        ]
        for args in cases:
            with self.subTest(command=args[0], count=len(args)):
                code, out, err = self.run_cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(self.calls, [])
                self.assertIn("预算", err)

    def test_yes_does_not_remove_limit(self):
        code, out, err = self.run_cli(["user", "alice", "bob", "--per-account", "--max", "1000", "--yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])

    def test_explicit_higher_budget_requires_confirmation(self):
        code, out, err = self.run_cli(["search", "AI", "--budget-usd", "0.50"])
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("--yes", err)

    def test_confirmed_budget_shared_by_accounts(self):
        code, out, err = self.run_cli(["user", "alice", "bob", "--per-account", "--max", "1000",
                                       "--budget-usd", "0.50", "--yes", "--format", "json"])
        meta = json.loads(out)["meta"]
        self.assertEqual(code, 0)
        self.assertEqual(meta["count"], 2000)
        self.assertEqual(meta["estimated_cost_usd"], .4)

    def test_filtered_timeline_stops_at_shared_budget(self):
        code, out, err = self.run_cli(["user", "alice", "--timeline", "--min-faves", "1",
                                       "--max", "900", "--format", "json"])
        meta = json.loads(out)["meta"]
        self.assertEqual(code, 2)
        self.assertEqual(meta["count"], 0)
        self.assertFalse(meta["complete"])
        self.assertLessEqual(meta["estimated_cost_usd"], .2)
        self.assertEqual(meta["estimated_cost_usd"], .1962)

    def test_profile_supports_explicit_budget(self):
        code, out, err = self.run_cli(["profile", *["u" + str(i) for i in range(1001)],
                                       "--budget-usd", ".30", "--yes", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["meta"]["estimated_cost_usd"], .2002)

    def test_unexpected_large_page_is_retained_then_stops(self):
        self.handler = lambda req: {"tweets": [tweet(i) for i in range(30)], "next_cursor": "more"}
        code, out, err = self.run_cli(["search", "AI", "--max", "1", "--budget-usd", ".004", "--format", "json"])
        meta = json.loads(out)["meta"]
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(meta["estimated_cost_usd"], .006)
        self.assertEqual(meta["count"], 1)
        self.assertFalse(meta["complete"])

    def test_partial_failure_retains_data_and_checkpoint(self):
        def fail_second(req):
            if len(self.calls) > 1:
                raise urllib.error.HTTPError(req.full_url, 402, "mock", {}, io.BytesIO(b'{"message":"Insufficient balance"}'))
            return self.respond(req)
        self.handler = fail_second
        with temporary_folder() as folder:
            output = Path(folder) / "result.json"
            code, out, err = self.run_cli(["search", "AI", "--format", "json", "--out", str(output)])
            data = json.loads(output.read_text(encoding="utf-8"))
            checkpoint = Path(str(output) + ".checkpoint.jsonl").read_text(encoding="utf-8")
            events = [json.loads(line) for line in checkpoint.splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(data["meta"]["count"], 20)
        self.assertEqual(data["meta"]["estimated_cost_usd"], .004)
        self.assertFalse(data["meta"]["complete"])
        self.assertEqual([e["event"] for e in events], ["start", "response", "finish"])
        self.assertNotIn("offline-test-placeholder", checkpoint)
        self.assertNotIn("Authorization", checkpoint)
        self.assertIn("0.0040", err)
        self.assertEqual(len(self.calls), 2)

    def test_interrupt_retains_first_page(self):
        def interrupt(req):
            if len(self.calls) > 1:
                raise KeyboardInterrupt
            return self.respond(req)
        self.handler = interrupt
        code, out, err = self.run_cli(["search", "AI", "--format", "json"])
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(out)["meta"]["count"], 20)
        self.assertFalse(json.loads(out)["meta"]["complete"])

    def test_network_uncertainty_consumes_budget_before_retry(self):
        def timeout(req):
            if len(self.calls) == 1:
                raise TimeoutError("mock")
            return self.respond(req)
        self.handler = timeout
        code, out, err = self.run_cli(["search", "AI", "--max", "20", "--budget-usd", ".004", "--format", "json"])
        meta = json.loads(out)["meta"]
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(meta["uncertain_cost_usd"], .004)

    def test_http_retry_policy(self):
        for status, expected in [(401, 1), (402, 1), (429, 4), (503, 4)]:
            with self.subTest(status=status):
                self.calls.clear()
                def error(req):
                    raise urllib.error.HTTPError(req.full_url, status, "mock", {}, io.BytesIO(b'{"message":"mock"}'))
                self.handler = error
                code, out, err = self.run_cli(["search", "AI", "--format", "json"])
                self.assertEqual(code, 1)
                self.assertEqual(len(self.calls), expected)

    def test_server_error_redacts_key(self):
        def error(req):
            raise urllib.error.HTTPError(req.full_url, 401, "mock", {}, io.BytesIO(b'{"message":"offline-test-placeholder"}'))
        self.handler = error
        code, out, err = self.run_cli(["search", "AI", "--format", "json"])
        self.assertNotIn("offline-test-placeholder", out + err)
        self.assertIn("[REDACTED]", out + err)

    def test_invalid_input_rejected_before_http(self):
        cases = [
            ["search", "AI", "--max", "-1"], ["search", "AI", "--max", "0"],
            ["user", "alice", "--batch", "0"], ["search", "AI", "--since-hours", "nan"],
            ["search", "AI", "--since-hours", "-1"], ["user", "alice", "--timeline", "--since-id", "invalid"],
            ["search", "AI", "--since", "bad-date"],
            ["search", "AI", "--since", "2026-10-02", "--until", "2026-10-01"],
            ["tweet", "1000000000000000001", "--comments", "-1"],
            ["profile", "bad/handle"], ["list", "invalid"],
            ["tweet", "1000000000000000001", "1000000000000000002", "--thread"],
            ["search", "AI", "--raw"], ["search", "AI", "--tz", "UTC+99:00"],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, out, err = self.run_cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(self.calls, [])

    def test_invalid_or_missing_timezone_fails_before_http(self):
        with patch.object(x, "ZoneInfo", side_effect=KeyError("missing database")):
            for zone in ["Not/AZone", "America/New_York"]:
                code, out, err = self.run_cli(["search", "AI", "--tz", zone])
                self.assertEqual(code, 1)
                self.assertEqual(self.calls, [])
                self.assertIn("tzdata", err)

    def test_fixed_offset_works_without_database(self):
        with patch.object(x, "ZoneInfo", side_effect=KeyError("missing database")):
            code, out, err = self.run_cli(["search", "AI", "--max", "1", "--since", "2026-10-01T08:00",
                                           "--tz", "UTC-04:00", "--format", "json"])
        self.assertEqual(code, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.calls[0].full_url).query)["query"][0]
        self.assertIn("since_time:1790856000", query)  # 2026-10-01 12:00 UTC

    def test_full_does_not_double_unique_count(self):
        code, out, err = self.run_cli(["tweet", "1000000000000000001", "--full", "--format", "json"])
        meta = json.loads(out)["meta"]
        self.assertEqual(code, 0)
        self.assertEqual(meta["unique_tweet_count"], 1)
        self.assertEqual(meta["count"], 1)
        self.assertEqual(meta["record_count"], 2)

    def test_jsonl_preserves_section_including_raw(self):
        for extra in [[], ["--raw"]]:
            with self.subTest(raw=bool(extra)):
                code, out, err = self.run_cli(["tweet", "1000000000000000001", "--thread", "--comments", "1",
                                               "--quotes", "1", "--max", "1", "--format", "jsonl", *extra])
                rows = [json.loads(line) for line in out.splitlines()]
                self.assertEqual(code, 0)
                self.assertEqual([r["section"] for r in rows], ["tweets", "thread", "comments", "quotes"])
                self.assertTrue(all(isinstance(r["data"], dict) for r in rows))

    def test_profile_jsonl_uses_same_envelope(self):
        code, out, err = self.run_cli(["profile", "alice", "--format", "jsonl"])
        self.assertEqual(json.loads(out)["section"], "users")

    def test_raw_text_is_preserved_and_display_is_separate(self):
        original = "A &amp; B https://t.co/media"
        self.handler = lambda req: tweet(1, original, entities={"media": [{"url": "https://t.co/media"}]})
        code, out, err = self.run_cli(["tweet", "1000000000000000001", "--format", "json"])
        record = json.loads(out)["tweets"][0]
        self.assertEqual(record["raw_text"], original)
        self.assertEqual(record["display_text"], "A & B")

    def test_csv_neutralizes_text_formulas_and_raw_is_explicit(self):
        for prefix in ["=1+1", "+1", "-1", "@SUM(1)", " =1+1", "\t=1+1", "＝1+1"]:
            with self.subTest(text=prefix):
                self.handler = lambda req: tweet(1, prefix, user={"screen_name": "example", "name": prefix})
                for mode in ["safe", "raw"]:
                    code, out, err = self.run_cli(["tweet", "1000000000000000001", "--format", "csv", "--csv-mode", mode])
                    row = next(csv.DictReader(io.StringIO(out)))
                    self.assertEqual(row["raw_text"], ("\t" if mode == "safe" else "") + prefix)
                    self.assertEqual(row["author_name"], ("\t" if mode == "safe" else "") + prefix)

    def test_profile_csv_neutralizes_description(self):
        self.handler = lambda req: {"id_str": "12345", "screen_name": "alice", "name": "=1+1", "description": "=1+1"}
        code, out, err = self.run_cli(["profile", "alice", "--format", "csv"])
        row = next(csv.DictReader(io.StringIO(out)))
        self.assertEqual(row["description"], "\t=1+1")

    def test_stream_file_has_metadata_sidecar(self):
        with temporary_folder() as folder:
            target = Path(folder) / "tweets.jsonl"
            code, out, err = self.run_cli(["search", "AI", "--max", "1", "--format", "jsonl", "--out", str(target)])
            meta = json.loads(Path(str(target) + ".meta.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["complete"])
            self.assertTrue(Path(str(target) + ".checkpoint.jsonl").exists())

    def test_output_checkpoint_collision_rejected(self):
        with temporary_folder() as folder:
            target = str(Path(folder) / "result")
            code, out, err = self.run_cli(["search", "AI", "--out", target, "--checkpoint", target])
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])

    def test_repeated_cursor_stops_incomplete(self):
        self.handler = lambda req: {"tweets": [tweet(1)], "next_cursor": "same"}
        code, out, err = self.run_cli(["search", "AI", "--max", "50", "--format", "json"])
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(json.loads(out)["meta"]["complete"])

    def test_reaching_target_ignores_unused_cursor(self):
        self.handler = lambda req: {"tweets": [tweet(len(self.calls))], "next_cursor": "same"}
        code, out, err = self.run_cli(["search", "AI", "--max", "2", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["meta"]["complete"])

    def test_malformed_successful_response_is_saved_as_uncertain(self):
        self.handler = lambda req: "unexpected response"
        code, out, err = self.run_cli(["profile", "alice", "bob", "--format", "json"])
        self.assertEqual(code, 1)
        meta = json.loads(out)["meta"]
        self.assertFalse(meta["complete"])
        self.assertEqual(meta["uncertain_cost_usd"], .0004)
        self.assertEqual(len(self.calls), 1)

    def test_checkpoint_cannot_overwrite_metadata_or_key_file(self):
        with temporary_folder() as folder:
            target = str(Path(folder) / "result.jsonl")
            key = Path(folder) / "local.env"
            key.write_text("SOCIALDATA_API_KEY=offline-test-placeholder\n", encoding="utf-8")
            for checkpoint in [target + ".meta.json", str(key)]:
                code, out, err = self.run_cli(["search", "AI", "--format", "jsonl", "--out", target,
                                               "--env-file", str(key), "--checkpoint", checkpoint])
                self.assertEqual(code, 1)
                self.assertEqual(self.calls, [])

    def test_checkpoint_redacts_key_echoed_in_invalid_response(self):
        self.handler = lambda req: {"error": "offline-test-placeholder"}
        with temporary_folder() as folder:
            output = str(Path(folder) / "result.json")
            code, out, err = self.run_cli(["profile", "alice", "--format", "json", "--out", output])
            checkpoint = Path(output + ".checkpoint.jsonl").read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertNotIn("offline-test-placeholder", checkpoint)
        self.assertIn("[REDACTED]", checkpoint)

    def test_scan_cap_is_marked_incomplete(self):
        code, out, err = self.run_cli(["user", "alice", "--timeline", "--max", "1", "--min-faves", "1", "--format", "json"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["meta"]["complete"])
        self.assertIn("已扫描", err)


class ConfigTests(unittest.TestCase):
    def test_environment_key_has_priority_over_missing_file(self):
        with patch.dict(os.environ, {"SOCIALDATA_API_KEY": "offline-test-placeholder"}):
            self.assertEqual(x.load_key("does-not-exist.env"), "offline-test-placeholder")

    def test_key_file_is_explicit_and_accepts_utf8_bom(self):
        with patch.dict(os.environ, {"SOCIALDATA_API_KEY": ""}), temporary_folder() as folder:
            target = Path(folder) / "local.env"
            target.write_text('SOCIALDATA_API_KEY="offline-test-placeholder"\n', encoding="utf-8-sig")
            self.assertEqual(x.load_key(str(target)), "offline-test-placeholder")
            with self.assertRaises(x.Fatal):
                x.load_key(None)

    def test_money_rejects_nonfinite_and_nonpositive_values(self):
        for value in ["nan", "Infinity", "-1", "0", "0.0001"]:
            with self.subTest(value=value), self.assertRaises(x.argparse.ArgumentTypeError):
                x.money(value)
        self.assertEqual(x.money("0.20"), Decimal("0.20"))


if __name__ == "__main__":
    unittest.main()
