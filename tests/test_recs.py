"""Run with:  python -m unittest discover -s tests   (no dependencies)"""
import json
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from recs import cli
from recs.extract import extract
from recs.merge import cluster, name_key
from recs.trades import resolve_query
from recs.whatsapp import first_name, parse_text

LRM, NNBSP = "‎", " "

IOS = (
    f"[3/1/26, 9:00:00{NNBSP}AM] Group: {LRM}Messages and calls are end-to-end encrypted.\r\n"
    f"[3/1/26, 9:01:00{NNBSP}AM] ~{NNBSP}Priya Shah: Any recommendations for a plumber? Leak under the sink\r\n"
    f"[3/1/26, 9:05:00{NNBSP}AM] ~{NNBSP}Tom Lee: Joe at Rapid Rooter is great.\nVery fair price\n(425) 555-0101\r\n"
    f"[3/1/26, 9:07:00{NNBSP}AM] ‪+1 (206) 555-7777‬: Same\r\n"
    f"[3/1/26, 9:20:00{NNBSP}AM] ~{NNBSP}Ana Ruiz: {LRM}Contact card omitted\r\n"
    f"[3/1/26, 9:20:30{NNBSP}AM] ~{NNBSP}Ana Ruiz: Mike is great, fixed our water heater\r\n"
    f"[3/1/26, 9:30:00{NNBSP}AM] ~{NNBSP}Ben Ok: Avoid Drip Kings. They never showed up and overcharged us\r\n"
    f"[3/1/26, 9:31:00{NNBSP}AM] ~{NNBSP}Ben Ok: {LRM}image omitted\r\n"
    f"[3/2/26, 8:00:00{NNBSP}AM] ~{NNBSP}Sam Cho: Hi all, recs for an electrician?\r\n"
    f"[3/2/26, 8:30:00{NNBSP}AM] ~{NNBSP}Lee Park: We used Bright Spark Electric, very prompt. brightsparkelectric.com\r\n"
)
# Android, 24h clock, day-first dates, same plumber under a different spelling and number format
ANDROID = (
    "14/03/2026, 18:02 - Kim Wu: Can anyone recommend a plumber?\n"
    "14/03/2026, 18:10 - Raj Rao: Rapid-Rooter (Joe) 425.555.0101 did a good job for us\n"
    "14/03/2026, 18:11 - Raj Rao: <Media omitted>\n"
    "15/03/2026, 09:00 - Kim Wu: thanks!\n"
)


class ParseTests(unittest.TestCase):
    def test_ios(self):
        msgs = parse_text(IOS)
        people = [m for m in msgs if not m.is_system]
        self.assertTrue(msgs[0].is_system)
        tom = next(m for m in people if m.sender == "Tom Lee")
        self.assertIn("\n(425) 555-0101", tom.text)  # multi-line message kept together
        self.assertTrue(any(m.is_media for m in people))
        self.assertEqual(first_name("+1 (206) 555-7777"), None)
        self.assertEqual(first_name("Jane's Business Number"), "Jane")
        self.assertEqual(tom.ts.hour, 9)

    def test_android_day_first(self):
        msgs = parse_text(ANDROID)
        self.assertEqual((msgs[0].ts.day, msgs[0].ts.month, msgs[0].ts.hour), (14, 3, 18))
        self.assertTrue(msgs[2].is_media)


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.m = extract(parse_text(IOS))

    def by(self, sender):
        return [x for x in self.m if x.sender == sender]

    def test_reply_inherits_trade_and_finds_person_at_business(self):
        (tom,) = self.by("Tom Lee")
        self.assertEqual((tom.pro_name, tom.person_name, tom.trade, tom.sentiment), ("Rapid Rooter", "Joe", "plumbing", "positive"))
        self.assertEqual(tom.phones, ["4255550101"])

    def test_plus_one_and_contact_card(self):
        (same,) = self.by("+1 (206) 555-7777")
        self.assertEqual(same.pro_name, "Rapid Rooter")
        (ana,) = self.by("Ana Ruiz")
        self.assertTrue(ana.is_card)
        self.assertEqual(ana.pro_name, "Mike")

    def test_negative(self):
        (ben,) = self.by("Ben Ok")
        self.assertEqual((ben.pro_name, ben.sentiment), ("Drip Kings", "negative"))

    def test_asker_is_not_a_recommender(self):
        self.assertFalse(self.by("Priya Shah"))

    def test_website(self):
        (lee,) = self.by("Lee Park")
        self.assertEqual(lee.trade, "electrical")
        self.assertTrue(lee.websites[0].endswith("brightsparkelectric.com"))


class MergeTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(name_key("Expert Irrigation Services"), name_key("Expert irrigation"))

    def test_phone_and_fuzzy(self):
        rows = [
            dict(id=1, neighbor_id=1, trade="landscaping", pro_name="Chris Moreno", person_name=None, phones=["2535550000"], websites=[], is_card=0, card_owner_id=None),
            dict(id=2, neighbor_id=2, trade="landscaping", pro_name="Christopher Morales", person_name=None, phones=["2535550000"], websites=[], is_card=0, card_owner_id=None),
            dict(id=3, neighbor_id=3, trade="roofing", pro_name="Rapid Rooter", person_name=None, phones=[], websites=[], is_card=0, card_owner_id=None),
            dict(id=4, neighbor_id=4, trade="holiday lights", pro_name="rapid rooters", person_name=None, phones=[], websites=["https://rapidrooterinc.com/"], is_card=0, card_owner_id=None),
            dict(id=5, neighbor_id=5, trade="handyman", pro_name="Joe", person_name=None, phones=[], websites=[], is_card=0, card_owner_id=None),
            dict(id=6, neighbor_id=6, trade="plumbing", pro_name="Joe", person_name=None, phones=[], websites=[], is_card=0, card_owner_id=None),
        ]
        c = cluster(rows)
        self.assertEqual(c[1], c[2])
        self.assertEqual(c[3], c[4])
        self.assertNotEqual(c[5], c[6])  # a bare first name only merges within one trade


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        os.environ["RECS_HOME"] = str(d)
        os.environ.pop("RECS_DB", None)
        z = d / "WhatsApp Chat - Maple Street.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("_chat.txt", IOS)
        (d / "WhatsApp Chat with Oak Lane.txt").write_text(ANDROID)
        self.files = [str(z), str(d / "WhatsApp Chat with Oak Lane.txt")]

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_import_rank_privacy(self):
        rc, out = self.run_cli("import", *self.files)
        self.assertEqual(rc, 0)
        self.assertIn("Maple Street", out)
        self.assertIn("Oak Lane", out)
        rc, out = self.run_cli("plumber")
        first = out.split("\n 2.")[0]
        self.assertIn("Rapid Rooter", first)          # 3 neighbours across 2 groups, merged by phone
        self.assertIn("3 neighbors recommend", first)
        self.assertIn("Drip Kings", out)
        self.assertIn("1 warned", out)
        _, again = self.run_cli("import", *self.files)  # idempotent
        self.assertIn("already imported", again)
        con = sqlite3.connect(Path(self.tmp.name) / "recs.db")
        dump = "\n".join(con.iterdump())
        self.assertNotIn("5557777", re.sub(r"\D", "", dump))  # a neighbour's own number never stored
        self.assertNotIn("Priya Shah", dump)                  # no full names
        names = {r[0] for r in con.execute("SELECT first_name FROM neighbors")}
        self.assertEqual(names, {"Tom", None, "Ana", "Ben", "Lee", "Raj"})

    def test_jsonl_with_explicit_threads(self):
        p = Path(self.tmp.name) / "fb.jsonl"
        rows = [
            {"id": "p1", "ts": "2026-05-01T10:00:00", "author": "Dana Fox", "text": "Looking for a roofer, any recommendations?"},
            {"id": "p2", "ts": "2026-05-01T10:05:00", "author": "Eli Gray", "text": "Anyone know a good dog walker?"},
            {"id": "c1", "reply_to": "p1", "ts": "2026-05-01T10:30:00", "author": "Fay Moss", "text": "Summit Roofing did ours, excellent work."},
        ]
        p.write_text("\n".join(json.dumps(r) for r in rows))
        self.run_cli("import", str(p), "--platform", "facebook", "--group", "Oak FB")
        _, out = self.run_cli("roofer")
        self.assertIn("Summit Roofing", out)  # threaded to the roofing post, not the newer dog-walker one

    def test_query_aliases(self):
        self.assertEqual(resolve_query("roofer"), ["roofing"])
        self.assertIn("hvac", resolve_query("furnace"))
        self.assertEqual(resolve_query("electricians"), ["electrical"])


if __name__ == "__main__":
    unittest.main()
