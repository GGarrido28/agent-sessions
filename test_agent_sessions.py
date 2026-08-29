"""Tests for the liveness logic in agent-sessions.

Run with `python -m unittest discover` or `python test_agent_sessions.py`.

Stdlib only, so the test suite carries the same "no dependencies" promise the
tool does. What is worth testing here is narrow: the ten lines that decide the
LIVE cell have been rewritten twice in response to sessions being labelled
wrongly, and every one of those bugs is reproducible from a transcript on disk
plus a clock. So the fixtures are transcripts, and each case is a session state
that was once reported as something it was not.
"""

import calendar
import importlib.machinery
import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LOADER = importlib.machinery.SourceFileLoader("ags", str(_HERE / "agent-sessions"))
_SPEC = importlib.util.spec_from_loader("ags", _LOADER)
ags = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(ags)


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".500Z"


def jsonl(records):
    """Compact, the way both tools write it -- the scan matches raw bytes.

    Bytes rather than text: write_text would turn these into CRLF on Windows
    and the fixtures would stop being byte-identical to a real transcript.
    """
    return "".join(
        json.dumps(r, separators=(",", ":")) + "\n" for r in records
    ).encode()



def patch(target, **values):
    """Set module globals for one test and put the originals back after.

    Without this a later test that forgets to patch would read the real
    ~/.codex and shell out to lsof or ps.
    """
    for name, value in values.items():
        original = getattr(ags, name)
        target.addCleanup(setattr, ags, name, original)
        setattr(ags, name, value)


class LastTs(unittest.TestCase):
    def test_reads_the_stamp(self):
        raw = b'{"timestamp":"2026-08-29T18:12:15.983Z","type":"event_msg"}'
        self.assertEqual(
            int(ags.last_ts(raw)),
            calendar.timegm((2026, 8, 29, 18, 12, 15, 0, 0, 0)),
        )

    def test_keeps_the_fraction(self):
        # Truncating to whole seconds sorts a Codex row below a Claude row
        # written in the same second, so the fraction has to survive.
        raw = b'{"timestamp":"2026-08-29T18:12:15.983Z"}'
        self.assertAlmostEqual(ags.last_ts(raw) % 1, 0.983, places=3)

    def test_keeps_the_fraction_of_an_offset_form_stamp(self):
        # Not a shape either tool writes today, but rstripping a trailing Z
        # would have swallowed the offset digits along with the fraction.
        raw = b'{"timestamp":"2026-08-29T18:12:15.983+00:00"}'
        self.assertAlmostEqual(ags.last_ts(raw) % 1, 0.983, places=3)

    def test_a_malformed_fraction_never_costs_the_second(self):
        second = calendar.timegm((2026, 8, 29, 18, 12, 15, 0, 0, 0))
        # Nothing to read: the second stands on its own.
        for raw in (
            b'{"timestamp":"2026-08-29T18:12:15.Z"}',
            b'{"timestamp":"2026-08-29T18:12:15,983Z"}',
        ):
            with self.subTest(raw=raw):
                self.assertEqual(ags.last_ts(raw), second)
        # A stamp torn mid-fraction keeps the digits it did get.
        self.assertAlmostEqual(
            ags.last_ts(b'{"timestamp":"2026-08-29T18:12:15.98xZ"}'),
            second + 0.98, places=3)

    def test_stamp_without_a_fraction(self):
        raw = b'{"timestamp":"2026-08-29T18:12:15Z"}'
        self.assertEqual(ags.last_ts(raw) % 1, 0)

    def test_takes_the_first_stamp_on_the_line(self):
        # Codex writes the record's own stamp first; a nested one must not win.
        raw = b'{"timestamp":"2026-08-29T18:12:15.000Z","payload":{"timestamp":"2020-01-01T00:00:00.000Z"}}'
        self.assertEqual(ags.last_ts(raw), calendar.timegm((2026, 8, 29, 18, 12, 15, 0, 0, 0)))

    def test_absent_and_malformed_fall_back(self):
        for raw in (
            None,
            b"",
            b'{"type":"last-prompt"}',            # Claude bookkeeping, no stamp
            b'{"timestamp":"not-a-date-at-all"}',
            b'{"timestamp":"2026-08-29T18:12"}',  # torn mid-stamp
            b'{"timestamp":"\xff\xfe\xff\xfe\xff\xfe\xff\xfe\xff\xfe\xff\xfe\xff\xfe\xff\xfe\xff\xfe\xff"}',
        ):
            self.assertIsNone(ags.last_ts(raw), raw)


class LiveCell(unittest.TestCase):
    """Each case is a real session state, checked with and without a lock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = self.tmp / "sessions" / "2026" / "08" / "29"
        self.day.mkdir(parents=True)
        self.locked = set()
        patch(self,
              CODEX_ROOT=self.tmp / "sessions",
              CLAUDE_ROOT=self.tmp / "claude",
              live_codex=lambda: self.locked,
              live_claude=lambda: {})
        ags._SCAN_CACHE.clear()
        self.addCleanup(ags._SCAN_CACHE.clear)
        self.n = 0

    def rollout(self, ends_with=None, quiet=5, mtime_age=None):
        """A rollout whose last record is `quiet` seconds old.

        `mtime_age` sets the file's mtime independently, which is how Windows
        actually behaves: it leaves the directory entry alone while the writer
        holds the file open, then stamps it on close.
        """
        self.n += 1
        uid = "00000000-0000-4000-8000-%012d" % self.n
        now = time.time()
        records = [
            {"timestamp": iso(now - quiet - 60), "type": "session_meta",
             "payload": {"cwd": "/tmp/demo", "thread_source": "user"}},
            {"timestamp": iso(now - quiet - 30), "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "t1"}},
            {"timestamp": iso(now - quiet), "type": "event_msg",
             "payload": {"type": ends_with or "token_count", "turn_id": "t1"}},
        ]
        path = self.day / ("rollout-2026-08-29T00-00-00-%s.jsonl" % uid)
        path.write_bytes(jsonl(records))
        stamp = now - (mtime_age if mtime_age is not None else quiet)
        os.utime(path, (stamp, stamp))
        return uid

    def live(self, uid, lock_held):
        self.locked = {uid} if lock_held else set()
        ags._SCAN_CACHE.clear()
        rows, _ = ags.collect({"codex"})
        row = next(r for r in rows if r["id"] == uid)
        return ags.live_state(row)

    def check(self, uid, without_lock, with_lock):
        self.assertEqual(self.live(uid, False), without_lock, "without a lock")
        self.assertEqual(self.live(uid, True), with_lock, "with a lock held")

    def test_mid_turn(self):
        self.check(self.rollout(quiet=5), "busy", "busy")

    def test_mid_turn_while_windows_holds_the_mtime_back(self):
        # The file was appended to 5s ago but stats 10 minutes old. Timed off
        # mtime this session drops out of the table while it is working.
        self.check(self.rollout(quiet=5, mtime_age=600), "busy", "busy")

    def test_turn_left_running_overnight(self):
        # Long silences inside a live turn are normal -- a build, a stalled
        # model. The lock proves the TUI is open, so this is still busy.
        self.check(self.rollout(quiet=7200), "-", "busy")

    def test_turn_ended_without_writing_a_completion(self):
        # ~5% of turns write neither task_complete nor turn_aborted, and that
        # shape stays on disk for good.
        #
        # Fifteen minutes of silence is past HEARTBEAT, so with no lock to go
        # on the session is treated as gone. With one it is still busy, and
        # deliberately so: a held lock cannot tell an abandoned turn apart from
        # a slow one, and calling a working session idle is the worse error of
        # the two. STALE_TURN catches it a day later -- see the next test.
        self.check(self.rollout(quiet=900), "-", "busy")

    def test_turn_abandoned_days_ago(self):
        self.check(self.rollout(quiet=5 * 86400), "-", "idle")

    def test_quit_an_hour_ago_with_the_mtime_flushed_on_close(self):
        # Closing the file stamps the mtime with the close time, so mtime says
        # "just now" for a session that stopped working an hour ago. Timed off
        # mtime this row reads busy; timed off the record it is an hour idle.
        uid = self.rollout(quiet=3600, mtime_age=1)
        rows, _ = ags.collect({"codex"})
        row = next(r for r in rows if r["id"] == uid)
        self.assertAlmostEqual(time.time() - row["last"], 3600, delta=5)
        self.assertEqual(self.live(uid, False), "-")

    def test_completed_turns(self):
        for ending in ("task_complete", "turn_aborted"):
            with self.subTest(ending=ending):
                self.check(self.rollout(ends_with=ending, quiet=5), "-", "idle")

    def test_ended_is_not_part_of_the_json_contract(self):
        self.rollout()
        rows, _ = ags.collect({"codex"})
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("ended", row)
            json.dumps(row)  # -j dumps these verbatim

    def test_a_second_collect_reads_the_same_row_from_cache(self):
        # The scan caches a copy of the row, and collect() mutates the copy it
        # hands back. A pop or a clamp reaching into the cache would show up as
        # the second call falling back to mtime.
        uid = self.rollout(quiet=3600, mtime_age=1)
        for call in range(3):
            rows, _ = ags.collect({"codex"})
            row = next(r for r in rows if r["id"] == uid)
            with self.subTest(call=call):
                self.assertNotIn("ended", row)
                self.assertAlmostEqual(time.time() - row["last"], 3600, delta=5)

    def test_an_epoch_zero_stamp_is_a_stamp_not_a_missing_one(self):
        self.n += 1
        uid = "00000000-0000-4000-8000-%012d" % self.n
        path = self.day / ("rollout-2026-08-29T00-00-00-%s.jsonl" % uid)
        path.write_bytes(jsonl([
            {"timestamp": "1970-01-01T00:00:00.000Z", "type": "session_meta",
             "payload": {"cwd": "/tmp/demo", "thread_source": "user"}},
        ]))
        rows, _ = ags.collect({"codex"})
        row = next(r for r in rows if r["id"] == uid)
        self.assertEqual(row["last"], 0)

    def test_last_is_never_in_the_future(self):
        uid = self.rollout(quiet=-3600)  # a stamp an hour ahead of the clock
        rows, _ = ags.collect({"codex"})
        row = next(r for r in rows if r["id"] == uid)
        self.assertLessEqual(row["last"], time.time())


class ClaudeRowsKeepMtime(unittest.TestCase):
    """Claude liveness comes from the process registry, not from `last`, and
    its transcripts routinely end on an untimestamped bookkeeping record."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        proj = self.tmp / "claude" / "C--tmp-demo"
        proj.mkdir(parents=True)
        patch(self,
              CLAUDE_ROOT=self.tmp / "claude",
              CODEX_ROOT=self.tmp / "none",
              live_claude=lambda: {},
              live_codex=lambda: set())
        ags._SCAN_CACHE.clear()
        self.addCleanup(ags._SCAN_CACHE.clear)
        self.path = proj / "cf3dd505-93c9-47b8-9a5c-dc6d43a83014.jsonl"

    def test_last_comes_from_mtime_not_the_final_record(self):
        old = time.time() - 90 * 86400
        self.path.write_bytes(jsonl([
            {"timestamp": iso(old), "type": "user", "promptSource": "typed",
             "cwd": "/tmp/demo", "message": {"role": "user", "content": "hi"}},
            {"type": "last-prompt", "lastPrompt": "hi", "sessionId": "x"},
        ]))
        recent = time.time() - 60
        os.utime(self.path, (recent, recent))
        rows, _ = ags.collect({"claude"})
        self.assertAlmostEqual(rows[0]["last"], recent, delta=2)


class StatusColour(unittest.TestCase):
    def cell(self, status):
        row = {"tool": "codex", "live": {"status": status}, "last": time.time(),
               "turns": 1, "cwd": "/tmp/demo", "branch": None, "agent": None,
               "title": "t", "prompt": "p", "reply": "r", "model": None}
        return ags.build_table([row], time.time(), False, 200, paint=True)[1]

    def test_an_unknown_status_reads_as_live_not_as_dead(self):
        # Claude Code writes `shell` today and will write others tomorrow.
        self.assertIn(ags.ANSI["live"], self.cell("shell"))

    def test_a_status_named_like_a_non_status_ansi_key(self):
        # `dim` is the colour of a dead row; a status must never take it. The
        # other cells on the row are legitimately dim, so check the LIVE cell.
        self.assertIn(ags.ANSI["live"] + "dim", self.cell("dim"))

    def test_known_statuses_keep_their_own_colour(self):
        for status in ("busy", "idle", "waiting"):
            with self.subTest(status=status):
                self.assertIn(ags.ANSI[status], self.cell(status))


if __name__ == "__main__":
    unittest.main()
