"""Focused offline regression tests for v0.3.11 confirmed-offer persistence, localized details and stable UI."""
import argparse
import importlib.util
import os
import tempfile
import threading
import time
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pla_mmo_seed_test", ROOT / "pla_mmo_seed.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
PNG = (ROOT / "pokemon-placeholder.png").read_bytes()


class FakeResponse:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, n=-1):
        return self.data[:n] if n >= 0 else self.data


class SpriteTests(unittest.TestCase):
    def setUp(self):
        mod._SPRITE_CACHE.clear()
        mod._SPRITE_MISSING_LOGGED.clear()
        mod._SPRITE_DOWNLOAD_ATTEMPTED.clear()
        mod._SPRITE_DOWNLOAD_IN_PROGRESS.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "plugin-data"
        self.cache.mkdir()
        self.local = self.root / "local-sprites"
        self.local.mkdir()
        self.detail = {"species_id": 395, "species_en": "Empoleon", "species": "帝王拿波"}
        self.patcher_data = patch.object(mod, "plugin_data", return_value=self.cache)
        self.patcher_roots = patch.object(mod, "_sprite_search_roots", return_value=[self.local])
        self.patcher_data.start()
        self.patcher_roots.start()
        self.addCleanup(self.patcher_data.stop)
        self.addCleanup(self.patcher_roots.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_image_signatures(self):
        self.assertEqual(mod._supported_image_bytes(PNG), ".png")
        self.assertIsNone(mod._supported_image_bytes(b"\x89PNG\r\n\x1a\ntruncated"))
        self.assertIsNone(mod._supported_image_bytes(b"<html>not an image</html>"))
        self.assertIsNone(mod._supported_image_bytes(b"RIFFxxxxWEBP"))

    def test_local_cache_hit_precedes_download(self):
        local_file = self.local / "395.png"
        local_file.write_bytes(PNG)
        with patch.object(mod.urllib.request, "urlopen", side_effect=AssertionError("network must not be called")):
            found = mod.find_cached_sprite(self.detail)
            self.assertEqual(found, local_file)

    def test_lookup_does_not_recursively_walk_sprite_roots(self):
        local_file = self.local / "395.png"
        local_file.write_bytes(PNG)
        with patch.object(Path, "rglob", side_effect=AssertionError("recursive scan must not run")):
            self.assertEqual(mod.find_cached_sprite(self.detail), local_file)

    def test_download_and_persistent_cache(self):
        with patch.object(mod.urllib.request, "urlopen", return_value=FakeResponse(PNG)) as fetch:
            found = mod.download_sprite(self.detail)
            self.assertTrue(found and found.is_file())
            self.assertTrue(mod._supported_image_file(found))
            self.assertEqual(found.read_bytes(), PNG)
            self.assertIn("legends-arceus/icons/395.png", fetch.call_args.args[0].full_url)
        with patch.object(mod.urllib.request, "urlopen", side_effect=AssertionError("cache should be reused")):
            again = mod.download_sprite(self.detail)
            self.assertEqual(again, found)

    def test_404_for_pla_icon_uses_generic_sprite_fallback(self):
        missing = urllib.error.HTTPError("https://example.invalid/icon.png", 404, "missing", {}, None)
        with patch.object(mod.urllib.request, "urlopen", side_effect=[missing, FakeResponse(PNG)]) as fetch:
            found = mod.download_sprite(self.detail)
        self.assertTrue(found and found.is_file())
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("/pokemon/395.png", fetch.call_args.args[0].full_url)

    def test_missing_network_falls_back_without_pillow(self):
        with patch.object(mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertIsNone(mod.download_sprite(self.detail))
        imports = (ROOT / "pla_mmo_seed.py").read_text(encoding="utf-8").splitlines()
        self.assertFalse(any("PIL" in line for line in imports if line.lstrip().startswith(("import ", "from "))))

    def test_panel_icon_copies_image_into_plugin_data(self):
        local_file = self.local / "395.png"
        local_file.write_bytes(PNG)
        result = Path(mod._panel_icon_path(self.detail))
        self.assertTrue(result.is_file())
        self.assertTrue(str(result).startswith(str(self.cache)))
        self.assertTrue(mod._supported_image_file(result))

    def test_panel_icon_returns_placeholder_without_waiting_for_download(self):
        mod._SPRITE_CACHE.clear()
        with patch.object(mod, "find_cached_sprite", return_value=None), \
             patch.object(mod, "_schedule_sprite_download") as schedule:
            start = time.monotonic()
            result = Path(mod._panel_icon_path(self.detail))
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.25)
        self.assertEqual(result.name, "pokemon-placeholder.png")
        schedule.assert_called_once_with(self.detail)

    def test_scheduler_does_not_wait_for_network_worker(self):
        started = threading.Event()
        release = threading.Event()
        def slow_download(detail):
            started.set()
            release.wait(1.0)
            return None
        with patch.object(mod, "download_sprite", side_effect=slow_download):
            start = time.monotonic()
            scheduled = mod._schedule_sprite_download(self.detail)
            elapsed = time.monotonic() - start
            self.assertTrue(scheduled)
            self.assertLess(elapsed, 0.25)
            self.assertTrue(started.wait(0.5))
            release.set()
        deadline = time.monotonic() + 1.0
        while mod._SPRITE_DOWNLOAD_IN_PROGRESS and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn((395, "empoleon"), mod._SPRITE_DOWNLOAD_IN_PROGRESS)

    def test_icon_cache_is_bounded(self):
        cache = self.cache / "sprite-cache"
        cache.mkdir()
        for index in range(130):
            path = cache / f"{index}-mon.png"
            path.write_bytes(PNG)
            os.utime(path, ns=(index + 1, index + 1))
        mod._prune_sprite_files(cache, keep=128)
        self.assertEqual(len(list(cache.glob("*.png"))), 128)
        self.assertTrue((cache / "129-mon.png").exists())
        self.assertTrue((cache / "128-mon.png").exists())

    def test_plugin_metadata_version(self):
        with (ROOT / "plugin.toml").open("rb") as stream:
            metadata = tomllib.load(stream)
        self.assertEqual(metadata["version"], "0.3.13")


class RecordRetentionTests(unittest.TestCase):
    def test_complete_and_partial_runs_share_a_12_record_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            runs.mkdir()
            base = time.time_ns() - 20_000_000_000
            complete_dirs = []
            for run_index in range(5):
                run_dir = runs / f"complete-{run_index}"
                mons = run_dir / "mons"
                mons.mkdir(parents=True)
                for mon_index in range(4):
                    path = mons / f"{mon_index + 1:02d}.pa8"
                    path.write_bytes(bytes([run_index + 1]) * 0x178)
                    timestamp = base + run_index * 1_000_000_000 + mon_index
                    os.utime(path, ns=(timestamp, timestamp))
                complete_dirs.append(run_dir)
            partial_dirs = []
            for run_index in range(3):
                run_dir = runs / f"partial-{run_index}"
                offers = run_dir / "confirmed-offers"
                offers.mkdir(parents=True)
                path = offers / "01_offer.pa8"
                path.write_bytes(bytes([20 + run_index]) * 0x178)
                timestamp = base + (10 + run_index) * 1_000_000_000
                os.utime(path, ns=(timestamp, timestamp))
                partial_dirs.append(run_dir)

            result = mod.prune_run_history(root, keep_pokemon=12, keep_incomplete_runs=2)
            kept_dirs = [path for path in runs.iterdir() if path.is_dir()]
            kept_records = sum(len(list(path.rglob("*.pa8"))) for path in kept_dirs)
            kept_incomplete = [path for path in kept_dirs if not (path / ".complete").exists()
                               and len(list((path / "mons").glob("*.pa8"))) < 4]
            self.assertLessEqual(kept_records, 12)
            self.assertEqual(kept_records, 12)
            self.assertLessEqual(len(kept_incomplete), 2)
            self.assertEqual(result["records_kept"], kept_records)
            self.assertGreater(result["records_deleted"], 0)

    def test_legacy_cursor_preview_dumps_are_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "runs" / "legacy"
            captures = run_dir / "captures"
            captures.mkdir(parents=True)
            (captures / "cursor-preview.pa8").write_bytes(b"preview")
            mod.prune_run_history(Path(temp), keep_pokemon=12, keep_incomplete_runs=2)
            self.assertFalse(captures.exists())

    def test_active_empty_run_is_protected_during_history_pruning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            runs.mkdir()
            now = time.time_ns()
            old_run = runs / "old-partial"
            old_offers = old_run / "confirmed-offers"
            old_offers.mkdir(parents=True)
            old_record = old_offers / "old.pa8"
            old_record.write_bytes(b"old")
            os.utime(old_record, ns=(now - 20_000_000_000, now - 20_000_000_000))

            newer_run = runs / "newer-partial"
            newer_offers = newer_run / "confirmed-offers"
            newer_offers.mkdir(parents=True)
            newer_record = newer_offers / "newer.pa8"
            newer_record.write_bytes(b"newer")
            os.utime(newer_record, ns=(now - 10_000_000_000, now - 10_000_000_000))

            active_run = runs / "active-empty"
            active_offers = active_run / "confirmed-offers"
            active_offers.mkdir(parents=True)

            result = mod.prune_run_history(
                root, keep_pokemon=12, keep_incomplete_runs=2, protected_run=active_run
            )
            self.assertTrue(active_run.is_dir())
            self.assertTrue(active_offers.is_dir())
            self.assertFalse(old_run.exists())
            self.assertTrue(newer_run.exists())
            self.assertEqual(result["incomplete_kept"], 2)

    def test_atomic_save_recreates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "fresh-run" / "confirmed-offers" / "001.pa8"
            saved = mod.atomic_save_pa8(target, b"valid-pa8-record")
            self.assertEqual(saved, target)
            self.assertEqual(target.read_bytes(), b"valid-pa8-record")
            self.assertFalse(target.with_suffix(target.suffix + ".tmp").exists())


class ConfirmedOfferHookTests(unittest.TestCase):
    def test_only_selector_four_confirmation_events_are_captured_once(self):
        import queue
        class FakeTradeBox:
            SELECTOR_OFFERING = 4
            def read_payload(self, payload):
                return payload
        fake_box = FakeTradeBox()
        class FakePLAPokemon:
            def decrypt(self, record):
                return record
            def read(self, record):
                return {"species": 395, "level": 35}
        host = types.SimpleNamespace(trade_box=fake_box, pla_pokemon=FakePLAPokemon())
        events = queue.Queue()
        original = fake_box.read_payload
        restore = mod.install_confirmed_offer_hook(host, events)
        self.assertIsNotNone(restore)

        preview = bytes([1]) * 0x178
        selected = bytes([2]) * 0x178
        # Cursor preview is selector 2 and must never be counted.
        fake_box.read_payload({"selector": 2, "record": preview})
        self.assertTrue(events.empty())

        # The selected Pokemon is offered on the confirmation page with selector 4.
        fake_box.read_payload({"selector": 4, "record": selected})
        captured, digest, _, hint = events.get_nowait()
        self.assertEqual(captured, selected)
        self.assertEqual(digest, mod.hashlib.sha256(selected).hexdigest())
        self.assertEqual(hint, {"species_id": 395, "level": 35})

        # A retransmission / repeated event for the same PA8 does not increment the count.
        fake_box.read_payload({"selector": 4, "record": selected})
        self.assertTrue(events.empty())

        # A different selected Pokemon is accepted as the next event.
        second = bytes([3]) * 0x178
        fake_box.read_payload({"selector": 4, "record": second})
        self.assertEqual(events.get_nowait()[0], second)
        restore()
        self.assertIs(fake_box.read_payload.__func__, original.__func__)

    def test_invalid_or_incomplete_records_are_ignored(self):
        import queue
        fake_box = types.SimpleNamespace(
            SELECTOR_OFFERING=4,
            read_payload=lambda payload: payload,
        )
        host = types.SimpleNamespace(trade_box=fake_box)
        events = queue.Queue()
        restore = mod.install_confirmed_offer_hook(host, events)
        fake_box.read_payload({"selector": 4, "record": b"short"})
        fake_box.read_payload({"selector": 4, "record": None})
        self.assertTrue(events.empty())
        restore()


class LeaveAndHostArgumentTests(unittest.TestCase):
    def test_graceful_leave_is_triggered_after_four_and_stops_grace_when_switch_left(self):
        class FakeHost:
            def build_parser(self):
                parser = argparse.ArgumentParser()
                parser.add_argument("--leave-after", type=float, default=60.0)
                parser.add_argument("--stay-after-leave", action="store_true")
                return parser
            def left_after_trade(self, participants):
                return participants == "switch-left"

        host = FakeHost()
        four_found = threading.Event()
        switch_left = threading.Event()
        restore = mod.install_graceful_leave_hooks(host, four_found, switch_left_event=switch_left)
        self.assertIsNotNone(restore)
        args = host.build_parser().parse_args([])
        self.assertEqual(args.leave_after, 60.0)
        self.assertTrue(host.left_after_trade("switch-left"))
        self.assertFalse(switch_left.is_set(), "B-return/ordinary session changes before 4/4 must not trigger final shutdown")
        four_found.set()
        self.assertEqual(args.leave_after, 0.0)
        self.assertTrue(args.stay_after_leave)
        self.assertTrue(host.left_after_trade("switch-left"))
        self.assertTrue(switch_left.is_set())
        self.assertFalse(args.stay_after_leave)
        restore()
        self.assertEqual(host.build_parser().parse_args([]).leave_after, 60.0)

    def test_host_arguments_do_not_enable_ambiguous_cursor_capture(self):
        args = types.SimpleNamespace(code="12345678", channel=6, seconds=1800)
        options = mod.host_arguments(args, Path("offer.pa8"), Path("captures"), Path("keys"))
        self.assertIn("--trade-box-record", options)
        self.assertNotIn("--trade-box-collect", options)

    def test_quiet_host_output_reports_leave_events_without_forwarding_packet_noise(self):
        import io
        target = io.StringIO()
        leave_sent = threading.Event()
        switch_left = threading.Event()
        output = mod.QuietHostOutput(target, on_leave_sent=leave_sent.set, on_switch_left=switch_left.set)
        output.write("[pla] -> 192.0.2.1: session leave request (type 3) x4\n")
        output.write("[pla] the console left after the trade; staying up\n")
        output.write("[Seed] 正在反推 Seed\n")
        self.assertTrue(leave_sent.is_set())
        self.assertTrue(switch_left.is_set())
        self.assertIn("[Seed] 正在反推 Seed", target.getvalue())
        self.assertNotIn("session leave request", target.getvalue())


class PokemonDetailDisplayTests(unittest.TestCase):
    def test_official_size_labels_use_pkhex_scalar_thresholds(self):
        self.assertEqual(mod._size_label_from_scalar(0), "XS")
        self.assertEqual(mod._size_label_from_scalar(0x0F), "XS")
        self.assertEqual(mod._size_label_from_scalar(0x10), "S")
        self.assertEqual(mod._size_label_from_scalar(0x30), "M")
        self.assertEqual(mod._size_label_from_scalar(0xD0), "L")
        self.assertEqual(mod._size_label_from_scalar(0xF0), "XL")
        self.assertEqual(mod._size_label_from_scalar(255), "XL")
        self.assertEqual(mod._size_label_from_scalar(None), "—")

    def test_compact_card_places_ids_and_extra_fields_on_right_without_headings(self):
        detail = {
            "species": "波皇子", "level": 35, "gender": "♀", "nature": "怕寂寞",
            "ball": "超级球", "ivs": [26, 16, 23, 9, 29, 26],
            "trainer_id": 519159, "secret_id": 2112, "ability": "激流", "size": "XL",
            "moves": ["泡沫光线", "水之波动", "冰冻之风", "啄"],
        }
        title, lines, chips, tone = mod._pokemon_panel_payload(2, detail)
        self.assertEqual(title, "02  波皇子 Lv.35")
        self.assertEqual(len(lines), 3)
        self.assertIn("519159 / 2112", lines[0])
        self.assertIn("♀ · 怕寂寞 · 超级球", lines[0])
        self.assertIn("个体值 26/16/23/9/29/26", lines[1])
        self.assertIn("激流 · XL", lines[1])
        self.assertIn("泡沫光线 · 水之波动", lines[2])
        self.assertIn("冰冻之风 · 啄", lines[2])
        self.assertTrue(all(line.startswith("\t") for line in lines))
        self.assertTrue(all("\u3000" in line for line in lines))
        for forbidden in ("表ID", "里ID", "特性", "体型", "招式"):
            self.assertFalse(any(forbidden in line for line in lines))
        right_groups = ("519159 / 2112", "激流 · XL", "冰冻之风 · 啄")
        right_starts = [
            mod._panel_display_units(line[1:line.rfind(right)])
            for line, right in zip(lines, right_groups)
        ]
        self.assertEqual(len(set(right_starts)), 1)

    def test_name_lookup_requests_simplified_chinese_from_pkhex(self):
        requests = []
        class FakeService:
            def _ask(self, request):
                requests.append(dict(request))
                if request.get("list") == "moves":
                    return {"names": [{"id": 1, "name": "拍击"}, {"id": 2, "name": "空手劈"}]}
                if request.get("list") == "abilities":
                    return {"names": [{"id": 67, "name": "激流"}]}
                return {"names": []}
        fake_root = types.ModuleType("pokeldn")
        fake_root.pokemon = types.SimpleNamespace(SERVICE=FakeService())
        mod._PKHEX_NAME_CACHE.clear()
        with patch.dict("sys.modules", {"pokeldn": fake_root}):
            self.assertEqual(mod._pkhex_name_by_id("moves", 1), "拍击")
            self.assertEqual(mod._pkhex_name_by_id("abilities", 67), "激流")
        self.assertTrue(requests)
        self.assertTrue(all(request.get("display_language") == "zh-Hans" for request in requests))
        mod._PKHEX_NAME_CACHE.clear()

    def test_english_ability_names_are_localized_to_simplified_chinese(self):
        with patch.object(mod, "_pkhex_name_by_id", return_value="Levitate"):
            self.assertEqual(mod.localized_ability_name("Levitate", 26), "飘浮")
        with patch.object(mod, "_pkhex_name_by_id", return_value="Torrent"):
            self.assertEqual(mod.localized_ability_name("Torrent", 67), "激流")
        with patch.object(mod, "_pkhex_name_by_id", return_value="激流"):
            self.assertEqual(mod.localized_ability_name("Torrent", 67), "激流")

    def test_summary_card_keeps_two_lines_and_two_chips_before_and_after_seed(self):
        waiting_lines, waiting_chips, _ = mod._summary_panel_payload(2, "collecting", phase="收录中")
        seed_lines, seed_chips, _ = mod._summary_panel_payload(
            4, "success", seed="14099244476027093035", phase="已完成"
        )
        self.assertEqual(len(waiting_lines), 2)
        self.assertEqual(len(seed_lines), 2)
        self.assertEqual(len(waiting_chips), 2)
        self.assertEqual(len(seed_chips), 2)

    def test_placeholder_and_filled_cards_have_same_three_line_height(self):
        _, placeholder_lines, _, _ = mod._pokemon_panel_payload(3, None)
        detail = {
            "species": "波皇子", "level": 35, "gender": "♀", "nature": "怕寂寞",
            "ball": "超级球", "ivs": [26, 16, 23, 9, 29, 26],
            "trainer_id": 519159, "secret_id": 2112, "ability": "激流", "size": "XL",
            "moves": ["泡沫光线", "水之波动", "冰冻之风", "啄"],
        }
        _, filled_lines, _, _ = mod._pokemon_panel_payload(3, detail)
        self.assertEqual(len(placeholder_lines), 3)
        self.assertEqual(len(filled_lines), 3)

    def test_pa8_raw_parser_extracts_ids_ability_moves_and_size(self):
        fake_root = types.ModuleType("pokeldn")
        fake_pla = types.ModuleType("pokeldn.pla")
        fake_pokemon = types.ModuleType("pokeldn.pla.pokemon")
        fake_pokemon.decrypt = lambda raw: raw
        fake_pla.pokemon = fake_pokemon
        fake_root.pla = fake_pla
        data = bytearray(0x178)
        data[0x0C:0x10] = (2_112_519_159).to_bytes(4, "little")
        data[0x14:0x16] = (67).to_bytes(2, "little")
        data[0x50] = 20
        data[0x51] = 90
        data[0x52] = 0xF0
        for offset, move_id in zip((0x54, 0x56, 0x58, 0x5A), (1, 2, 3, 4)):
            data[offset:offset+2] = move_id.to_bytes(2, "little")
        data[0x137] = 4
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / "record.pa8"
            record.write_bytes(data)
            with patch.dict("sys.modules", {"pokeldn": fake_root, "pokeldn.pla": fake_pla,
                                             "pokeldn.pla.pokemon": fake_pokemon}):
                summary = mod._read_pa8_summary(record)
        self.assertEqual(summary["trainer_id"], 519159)
        self.assertEqual(summary["secret_id"], 2112)
        self.assertEqual(summary["ability_id"], 67)
        self.assertEqual(summary["move_ids"], [1, 2, 3, 4])
        self.assertEqual(summary["size"], "XL")
        self.assertEqual(summary["ball"], "精灵球")



class SolverStatusTests(unittest.TestCase):
    def test_no_group_seed_returns_failure_even_when_solver_process_exits_zero(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = iter(["No group seeds found with the input data. Double check your inputs.\n"])
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            solver_dir = root / "solver"
            solver_dir.mkdir()
            exe = solver_dir / "EtumrepMMO.ConsoleApp.1.2.0.exe"
            exe.write_bytes(b"fake")
            mons = root / "mons"
            mons.mkdir()
            for index in range(4):
                (mons / f"{index + 1:02d}.pa8").write_bytes(bytes([index + 1]) * 0x178)
            with patch.object(mod, "find_solver", return_value=exe), \
                 patch.object(mod, "update_collection_ui"), \
                 patch.object(mod, "plugin_data", return_value=root), \
                 patch.object(mod.subprocess, "Popen", return_value=FakeProcess()):
                code = mod.run_solver(root, mons)
        self.assertNotEqual(code, 0)


class UIRenderLockTests(unittest.TestCase):
    def test_cards_render_without_synchronous_clear_and_unchanged_cards_are_not_redrawn(self):
        calls = []
        class FakePanel:
            def clear(self, card_id):
                raise AssertionError("panel.clear must not block the first render")
            def show(self, card_id, title, lines, **kwargs):
                calls.append(("show", card_id, title, tuple(lines), kwargs.get("image")))
        class FakeProgress:
            def show(self, *args, **kwargs):
                pass
        fake = types.ModuleType("pokeldn")
        fake.panel = FakePanel()
        fake.progress = FakeProgress()
        detail = {"species": "帝王拿波", "species_en": "Empoleon", "level": 64,
                  "gender": "♀", "nature": "内敛", "ball": "精灵球", "ivs": [31, 20, 25, 30, 18, 22]}
        mod.reset_collection_panel_state()
        with patch.dict("sys.modules", {"pokeldn": fake}), \
             patch.object(mod, "_panel_icon_path", return_value=None), \
             patch.object(mod, "record_details", return_value=detail), \
             patch.object(mod, "write_pokemon_details_file", return_value=None):
            mod.update_collection_ui(0, "waiting")
            mod.update_collection_ui(1, "one", "info", ["fake.pa8"])
            # Repeating the same state must not redraw any card.
            before = len(calls)
            mod.update_collection_ui(1, "one", "info", ["fake.pa8"])
            self.assertEqual(len(calls), before)

        self.assertFalse(any(item[0] == "clear" for item in calls))
        show_events = [item for item in calls if item[0] == "show"]
        show_ids = [item[1] for item in show_events]
        self.assertEqual(show_ids.count("seed-capture"), 2)
        self.assertEqual(show_ids.count("seed-mon-1"), 2)
        self.assertEqual(show_ids.count("seed-mon-2"), 1)
        self.assertEqual(show_ids.count("seed-mon-4"), 1)
        row1_titles = [item[2] for item in show_events if item[1] == "seed-mon-1"]
        self.assertIn("01  帝王拿波 Lv.64", row1_titles)


    def test_one_card_render_error_does_not_prevent_other_cards(self):
        calls = []
        class FakePanel:
            def clear(self, card_id):
                raise AssertionError("panel.clear must not be called")
            def show(self, card_id, title, lines, **kwargs):
                calls.append(card_id)
                if card_id == "seed-mon-2":
                    raise RuntimeError("simulated card failure")
        fake = types.ModuleType("pokeldn")
        fake.panel = FakePanel()
        fake.progress = types.SimpleNamespace(show=lambda *args, **kwargs: None)
        mod.reset_collection_panel_state()
        with patch.dict("sys.modules", {"pokeldn": fake}), \
             patch.object(mod, "_panel_icon_path", return_value=None), \
             patch.object(mod, "record_details", return_value=None), \
             patch.object(mod, "write_pokemon_details_file", return_value=None):
            mod.update_collection_ui(0, "waiting")
        self.assertIn("seed-capture", calls)
        self.assertIn("seed-mon-1", calls)
        self.assertIn("seed-mon-2", calls)
        self.assertIn("seed-mon-3", calls)
        self.assertIn("seed-mon-4", calls)

    def test_concurrent_ui_updates_are_serialized(self):
        calls = []
        class FakePanel:
            def clear(self, card_id):
                calls.append(("clear", card_id))
            def show(self, card_id, title, lines, **kwargs):
                calls.append(("show", card_id, title))
                time.sleep(0.001)
        fake = types.ModuleType("pokeldn")
        fake.panel = FakePanel()
        fake.progress = types.SimpleNamespace(show=lambda *args, **kwargs: None)
        mod.reset_collection_panel_state()
        with patch.dict("sys.modules", {"pokeldn": fake}), \
             patch.object(mod, "_panel_icon_path", return_value=None), \
             patch.object(mod, "record_details", return_value={"species": "波皇子", "level": 35,
                 "gender": "♀", "nature": "怕寂寞", "ball": "超级球", "ivs": [1, 2, 3, 4, 5, 6]}), \
             patch.object(mod, "write_pokemon_details_file", return_value=None):
            threads = [
                threading.Thread(target=mod.update_collection_ui, args=(0, "waiting")),
                threading.Thread(target=mod.update_collection_ui, args=(1, "one", "info", ["fake.pa8"])),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2.0)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        show_events = [item for item in calls if item[0] == "show"]
        show_ids = [item[1] for item in show_events]
        self.assertEqual(show_ids.count("seed-capture"), 2)
        self.assertEqual(show_ids.count("seed-mon-1"), 2)
        # Unchanged rows are deliberately skipped, rather than cleared/redrawn.
        self.assertEqual(show_ids.count("seed-mon-4"), 1)
        self.assertEqual(len([item for item in calls if item[0] == "clear"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
