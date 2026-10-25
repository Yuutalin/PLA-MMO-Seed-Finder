#!/usr/bin/env python3
"""Pokeldn PLA MMO Seed collector and EtumrepMMO bridge."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path


PLUGIN_ID = "junli-pla-mmo-seed"
SOLVER_EXE_NAMES = (
    "EtumrepMMO.ConsoleApp.exe",
    "EtumrepMMO.exe",
)

# Official release ZIPs may keep the version in the executable filename,
# e.g. EtumrepMMO.ConsoleApp.1.2.0.exe.
SOLVER_EXE_GLOBS = (
    "EtumrepMMO.ConsoleApp.*.exe",
    "EtumrepMMO.*.exe",
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="收集《宝可梦传说 阿尔宙斯》最初四只宝可梦记录，并反推 MMO 群体 Seed。"
    )
    parser.add_argument("--mode", choices=("collect", "solve-last"), default="collect")
    parser.add_argument("--code", default="00000000")
    parser.add_argument("--channel", type=int, default=6)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--keys", default="")
    parser.add_argument("--trainer", default="")
    parser.add_argument("--stamp", default="")
    return parser


def trainer_from_arg(value):
    parts = (value or "").split("|", 4)
    if len(parts) != 5:
        return {"ot": "POKELDN", "tid": 0, "sid": 0, "language": 2, "gender": 0}

    def num(raw, fallback):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    return {
        "ot": parts[0] or "POKELDN",
        "tid": num(parts[1], 0),
        "sid": num(parts[2], 0),
        "language": num(parts[3], 2),
        "gender": num(parts[4], 0),
    }


def plugin_data():
    from pokeldn.plugins import data_folder
    return Path(data_folder(PLUGIN_ID))


def prune_run_history(root=None, keep_pokemon=12, keep_incomplete_runs=2, protected_run=None):
    """Keep the newest N selected PA8 records, counting partial sessions too.

    Complete runs retain a marker so trimming older history to a rolling record window
    does not make those runs look like active/incomplete sessions. The newest complete
    run remains intact for solve-last; old runs may retain fewer than four historical
    records once they fall outside the newest-N window.
    """
    root = Path(root or plugin_data())
    protected_path = Path(protected_run).resolve() if protected_run is not None else None
    runs_dir = root / "runs"
    if not runs_dir.exists():
        return {"complete_kept": 0, "complete_deleted": 0,
                "incomplete_kept": 0, "incomplete_deleted": 0,
                "records_kept": 0, "records_deleted": 0}

    run_info = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        mons_dir = run_dir / "mons"
        mons_files = sorted(p for p in mons_dir.glob("*.pa8") if p.is_file()) if mons_dir.exists() else []
        confirmed_dir = run_dir / "confirmed-offers"
        confirmed_files = sorted(p for p in confirmed_dir.glob("*.pa8") if p.is_file()) if confirmed_dir.exists() else []
        complete_marker = run_dir / ".complete"
        is_complete = complete_marker.exists() or len(mons_files) >= 4
        if is_complete and not complete_marker.exists():
            try:
                complete_marker.write_text("complete\n", encoding="utf-8")
            except OSError:
                pass

        # Old versions saved cursor previews to captures/. They cannot reliably tell
        # which Pokemon was confirmed, so delete them instead of counting them as history.
        legacy_captures = run_dir / "captures"
        if legacy_captures.exists():
            try:
                shutil.rmtree(legacy_captures)
            except OSError as exc:
                print(f"[Seed 历史清理警告] 无法清理旧版光标预览目录 {legacy_captures}: {exc}", flush=True)

        if is_complete:
            # mons/ is canonical after a successful run; remove any staging duplicates.
            if confirmed_dir.exists():
                try:
                    shutil.rmtree(confirmed_dir)
                except OSError as exc:
                    print(f"[Seed 历史清理警告] 无法清理临时记录目录 {confirmed_dir}: {exc}", flush=True)
            record_files = mons_files
        else:
            # If saving to mons/ was interrupted, the confirmed-offers copy is canonical.
            if confirmed_files and mons_files:
                for partial in mons_files:
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                mons_files = []
            record_files = confirmed_files or mons_files

        try:
            newest_time = max((p.stat().st_mtime_ns for p in record_files), default=run_dir.stat().st_mtime_ns)
        except OSError:
            newest_time = run_dir.stat().st_mtime_ns
        try:
            is_protected = protected_path is not None and run_dir.resolve() == protected_path
        except OSError:
            is_protected = protected_path is not None and run_dir == Path(protected_run)
        run_info.append({
            "time": newest_time, "path": run_dir, "complete": is_complete,
            "record_files": list(record_files), "protected": is_protected,
        })

    run_info.sort(key=lambda item: item["time"], reverse=True)
    # Never prune the active collection session, even before its first record exists.
    # Count it against the incomplete-session limit so only (limit - 1) old partial
    # sessions are retained alongside the active one.
    protected_incomplete = any(item["protected"] and not item["complete"] for item in run_info)
    incomplete_runs = [item for item in run_info if not item["complete"] and not item["protected"]]
    incomplete_limit = max(0, int(keep_incomplete_runs) - (1 if protected_incomplete else 0))
    deleted_complete = 0
    deleted_incomplete = 0
    deleted_records = 0
    for run in incomplete_runs[incomplete_limit:]:
        try:
            record_count = len(run["record_files"])
            shutil.rmtree(run["path"])
            deleted_incomplete += 1
            deleted_records += record_count
            run_info.remove(run)
        except OSError as exc:
            print(f"[Seed 历史清理警告] 无法删除旧未完成采集 {run['path']}: {exc}", flush=True)

    # Keep the newest N individual records across both completed and partial runs,
    # but never delete records from the active run while collection is in progress.
    all_records = []
    for run in run_info:
        for record in run["record_files"]:
            try:
                mtime = record.stat().st_mtime_ns
            except OSError:
                mtime = run["time"]
            all_records.append((mtime, record, run))
    all_records.sort(key=lambda item: item[0], reverse=True)
    keep_limit = max(1, int(keep_pokemon))
    protected_records = {
        record for _, record, run in all_records if run.get("protected")
    }
    other_records = [item for item in all_records if item[1] not in protected_records]
    slots_for_history = max(0, keep_limit - len(protected_records))
    keep_paths = protected_records | {record for _, record, _ in other_records[:slots_for_history]}
    for _, record, run in all_records:
        if record in keep_paths or run.get("protected"):
            continue
        try:
            record.unlink()
            deleted_records += 1
        except OSError as exc:
            print(f"[Seed 历史清理警告] 无法删除超出保留窗口的记录 {record}: {exc}", flush=True)

    # Remove empty temporary directories and runs that no longer hold any selected record.
    for run in list(run_info):
        if run.get("protected"):
            continue
        path = run["path"]
        subdirs_to_remove = [path / "captures"]
        if run["complete"]:
            subdirs_to_remove.append(path / "confirmed-offers")
        for subdir in subdirs_to_remove:
            if subdir.exists():
                try:
                    shutil.rmtree(subdir)
                except OSError:
                    pass
        partial_offers = path / "confirmed-offers"
        if partial_offers.exists() and not any(partial_offers.glob("*.pa8")):
            try:
                shutil.rmtree(partial_offers)
            except OSError:
                pass
        try:
            remaining = [p for p in path.rglob("*.pa8") if p.is_file()]
        except OSError:
            remaining = []
        if not remaining:
            try:
                shutil.rmtree(path)
                if run["complete"]:
                    deleted_complete += 1
                else:
                    deleted_incomplete += 1
                run_info.remove(run)
            except OSError:
                pass

    records_kept = sum(
        1 for run in run_info if run["path"].exists()
        for record in run["path"].rglob("*.pa8") if record.is_file()
    )
    complete_kept = sum(1 for run in run_info if run["complete"] and run["path"].exists())
    incomplete_kept = sum(1 for run in run_info if not run["complete"] and run["path"].exists())
    return {
        "complete_kept": complete_kept,
        "complete_deleted": deleted_complete,
        "incomplete_kept": incomplete_kept,
        "incomplete_deleted": deleted_incomplete,
        "records_kept": records_kept,
        "records_deleted": deleted_records,
    }


def atomic_save_pa8(target, record):
    """Atomically save a confirmed PA8 record, recreating its parent if needed."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    try:
        temp.write_bytes(record)
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def install_confirmed_offer_hook(host, event_queue):
    """Capture only selector-4 offered records, i.e. the game's trade-confirmation step.

    The PLA host receives selector 2 while the cursor previews a Pokemon and selector 4
    when the player actually offers that Pokemon on the trade-confirmation screen. The
    raw PA8 payload is the same for both selectors, so selection must be keyed on the
    selector event, never on a quiet timer or cursor dwell time.
    """
    trade_box = getattr(host, "trade_box", None)
    original = getattr(trade_box, "read_payload", None)
    if trade_box is None or not callable(original):
        return None

    selector_offering = getattr(trade_box, "SELECTOR_OFFERING", 4)
    seen_digests = set()
    seen_lock = threading.Lock()

    def wrapped_read_payload(payload):
        offered = original(payload)
        if not isinstance(offered, dict):
            return offered
        try:
            selector = int(offered.get("selector", -1))
            record = offered.get("record")
        except (TypeError, ValueError):
            return offered
        if selector != selector_offering or not isinstance(record, (bytes, bytearray)):
            return offered
        # PLA's captured party record is a 0x178-byte PA8. Reject incomplete payloads.
        if len(record) != 0x178:
            return offered
        record = bytes(record)
        digest = hashlib.sha256(record).hexdigest()
        with seen_lock:
            if digest in seen_digests:
                return offered
            seen_digests.add(digest)
        # Capture a small, local-only hint for a descriptive temporary filename. The
        # actual UI fields are still decoded from the saved PA8 record afterwards.
        hint = {}
        pla_pokemon = getattr(host, "pla_pokemon", None)
        try:
            if pla_pokemon and callable(getattr(pla_pokemon, "decrypt", None)) and callable(getattr(pla_pokemon, "read", None)):
                fields = pla_pokemon.read(pla_pokemon.decrypt(record))
                if isinstance(fields, dict):
                    hint = {"species_id": fields.get("species"), "level": fields.get("level")}
        except Exception:
            hint = {}
        # Only the selector-4 event enters the queue. Cursor previews (selector 2),
        # retransmissions, and repeated selections of the same record cannot count.
        event_queue.put((record, digest, time.monotonic(), hint))
        return offered

    trade_box.read_payload = wrapped_read_payload

    def restore():
        trade_box.read_payload = original

    return restore


NATURE_NAMES = (
    "勤奋", "怕寂寞", "勇敢", "固执", "顽皮", "大胆", "坦率", "悠闲", "淘气", "乐天",
    "胆小", "急躁", "认真", "爽朗", "天真", "内敛", "慢吞吞", "冷静", "害羞", "马虎",
    "温和", "温顺", "自大", "慎重", "浮躁",
)


BALL_NAMES = {
    0: "未知球", 1: "大师球", 2: "高级球", 3: "超级球", 4: "精灵球",
    5: "狩猎球", 6: "捕网球", 7: "潜水球", 8: "巢穴球", 9: "重复球",
    10: "计时球", 11: "豪华球", 12: "纪念球", 13: "黑暗球", 14: "治愈球",
    15: "先机球", 16: "贵重球", 17: "速度球", 18: "等级球", 19: "诱饵球",
    20: "沉重球", 21: "甜蜜球", 22: "友友球", 23: "月亮球", 24: "竞赛球",
    25: "梦境球", 26: "究极球", 27: "奇异球",
}


def _size_label_from_scalar(value):
    """Return PKHeX's official XS/S/M/L/XL size rating for the PA8 scale byte."""
    try:
        scalar = int(value)
    except (TypeError, ValueError):
        return "—"
    if not 0 <= scalar <= 255:
        return "—"
    # Mirrors PKHeX.Core PokeSizeUtil.GetSizeRating thresholds.
    if scalar < 0x10:
        return "XS"
    if scalar < 0x30:
        return "S"
    if scalar < 0xD0:
        return "M"
    if scalar < 0xF0:
        return "L"
    return "XL"


def _pkhex_name_by_id(kind, identifier):
    """Resolve a move/ability ID through Pokeldn's bundled PKHeX name catalog."""
    try:
        identifier = int(identifier)
    except (TypeError, ValueError):
        return ""
    if identifier <= 0:
        return ""
    cache_key = str(kind)
    cached = _PKHEX_NAME_CACHE.get(cache_key)
    if cached is None:
        cached = {}
        try:
            from pokeldn import pokemon as pokemon_service
            variants = ("moves", "move") if kind == "moves" else ("abilities", "ability")
            for variant in variants:
                try:
                    # The bundled PKHeX service supports zh-Hans for game names. Prefer this
                    # over SERVICE.names(), whose public convenience method defaults to English.
                    ask = getattr(pokemon_service.SERVICE, "_ask", None)
                    if callable(ask):
                        reply = ask({"cmd": "names", "game": "pla", "list": variant,
                                     "display_language": "zh-Hans"})
                        rows = reply.get("names", []) if isinstance(reply, dict) else []
                    else:
                        rows = pokemon_service.SERVICE.names("pla", variant)
                    for row in rows or []:
                        if not isinstance(row, dict):
                            continue
                        row_id = row.get("id", row.get("move_id", row.get("ability_id")))
                        row_name = row.get("name") or row.get("label")
                        try:
                            row_id = int(row_id)
                        except (TypeError, ValueError):
                            continue
                        if row_name and row_id > 0:
                            cached[row_id] = str(row_name)
                    if cached:
                        break
                except Exception:
                    continue
        except Exception:
            pass
        _PKHEX_NAME_CACHE[cache_key] = cached
    return cached.get(identifier, "")


def _read_pa8_summary(path):
    """Read PLA PA8 fields that the current PKHeX summary API may omit.

    IVs are returned in HP / Atk / Def / SpA / SpD / Spe order. Trainer ID uses
    PLA's visible ID (ID32 mod 1,000,000) and secret ID (ID32 div 1,000,000).
    Size labels mirror PKHeX's XS/S/M/L/XL scale rating.
    """
    summary = {"ball": "未知球", "ivs": None, "extra": {}, "trainer_id": None,
               "secret_id": None, "ability_id": None, "move_ids": [], "size": "—"}
    try:
        from pokeldn.pla import pokemon as pla_pokemon
        raw = Path(path).read_bytes()
        plain = pla_pokemon.decrypt(raw)
        if not isinstance(plain, (bytes, bytearray)) or len(plain) < 0x138:
            return summary
        packed_ivs = int.from_bytes(plain[0x94:0x98], "little")
        hp = packed_ivs & 0x1F
        atk = (packed_ivs >> 5) & 0x1F
        defense = (packed_ivs >> 10) & 0x1F
        speed = (packed_ivs >> 15) & 0x1F
        sp_atk = (packed_ivs >> 20) & 0x1F
        sp_def = (packed_ivs >> 25) & 0x1F
        summary["ivs"] = [hp, atk, defense, sp_atk, sp_def, speed]
        ball_id = plain[0x137]
        summary["ball"] = BALL_NAMES.get(ball_id, f"未知球（{ball_id}）")
        id32 = int.from_bytes(plain[0x0C:0x10], "little")
        summary["trainer_id"] = id32 % 1_000_000
        summary["secret_id"] = id32 // 1_000_000
        summary["ability_id"] = int.from_bytes(plain[0x14:0x16], "little")
        summary["move_ids"] = [int.from_bytes(plain[offset:offset + 2], "little")
                                for offset in (0x54, 0x56, 0x58, 0x5A)]
        summary["size"] = _size_label_from_scalar(plain[0x52])
        summary["extra"] = {
            "精灵球编号": ball_id,
            "个体值顺序": "HP / 攻击 / 防御 / 特攻 / 特防 / 速度",
            "体型标量": plain[0x52],
            "身高标量": plain[0x50],
            "体重标量": plain[0x51],
            "特性编号": summary["ability_id"],
            "招式编号": summary["move_ids"],
        }
    except Exception as exc:
        warning_key = ("pa8-summary", str(path))
        if warning_key not in _DETAIL_PARSE_WARNINGS:
            _DETAIL_PARSE_WARNINGS.add(warning_key)
            print(f"[Seed 信息解析] 无法从 {Path(path).name} 解码精灵球/个体值/扩展属性：{type(exc).__name__}: {exc}", flush=True)
    return summary


def write_pokemon_details_file(records, details=None, root=None):
    """Save optional extended information for reference without cluttering the main card."""
    records = list(records or [])[:4]
    if not records:
        return None
    root = Path(root or plugin_data())
    root.mkdir(parents=True, exist_ok=True)
    details = list(details or [record_details(path) for path in records])
    rows = []
    text_lines = [
        "阿尔宙斯 MMO Seed 助手 - 本次宝可梦详细信息",
        "个体值顺序：HP / 攻击 / 防御 / 特攻 / 特防 / 速度",
        "",
    ]
    for index, (path, detail) in enumerate(zip(records, details), start=1):
        ivs = detail.get("ivs")
        row = {
            "序号": index,
            "宝可梦": detail.get("species", "未知宝可梦"),
            "等级": detail.get("level", "?"),
            "性别": detail.get("gender", "未知"),
            "性格": detail.get("nature", "未知"),
            "精灵球": detail.get("ball", "未知球"),
            "个体值（HP/攻击/防御/特攻/特防/速度）": ivs,
            "表ID": detail.get("trainer_id"),
            "里ID": detail.get("secret_id"),
            "记录特性字段": detail.get("ability", "—"),
            "官方体型等级": detail.get("size", "—"),
            "招式": detail.get("moves", []),
            "闪光": bool(detail.get("shiny", False)),
            "头目": bool(detail.get("alpha", False)),
            "记录文件": str(path),
            "其他可用元数据": detail.get("extra", {}),
        }
        rows.append(row)
        iv_text = "/".join(str(v) for v in ivs) if isinstance(ivs, (list, tuple)) and len(ivs) == 6 else "暂不可用"
        text_lines.extend([
            f"{index:02d}. {row['宝可梦']} Lv.{row['等级']}",
            f"    性别：{row['性别']}；性格：{row['性格']}；精灵球：{row['精灵球']}",
            f"    个体值：{iv_text}",
            f"    表ID/里ID：{row['表ID'] if row['表ID'] is not None else '—'} / {row['里ID'] if row['里ID'] is not None else '—'}",
            f"    记录特性字段：{row['记录特性字段']}；体型：{row['官方体型等级']}",
            f"    招式：{' / '.join(row['招式']) if row['招式'] else '—'}",
            f"    闪光：{'是' if row['闪光'] else '否'}；头目：{'是' if row['头目'] else '否'}",
            f"    原始记录：{row['记录文件']}",
        ])
        extra = row.get("其他可用元数据") or {}
        if extra:
            text_lines.append("    其他元数据：" + json.dumps(extra, ensure_ascii=False, default=str))
        text_lines.append("")

    txt_target = root / "latest_pokemon_details.txt"
    json_target = root / "latest_pokemon_details.json"
    try:
        txt_target.write_text("\n".join(text_lines), encoding="utf-8")
        json_target.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return txt_target
    except OSError as exc:
        print(f"[Seed 详情] 无法保存宝可梦详细信息：{exc}", flush=True)
        return None



ABILITY_EN_TO_ZH = {
    "adaptability": "适应力", "aftermath": "引爆", "anger point": "愤怒穴位",
    "anticipation": "危险预知", "arena trap": "沙穴", "battle armor": "战斗盔甲",
    "big pecks": "健壮胸肌", "blaze": "猛火", "chlorophyll": "叶绿素",
    "clear body": "恒净之躯", "cloud nine": "无关天气", "competitive": "好胜",
    "compound eyes": "复眼", "contrary": "唱反调", "cute charm": "迷人之躯",
    "damp": "湿气", "defiant": "不服输", "download": "下载", "drizzle": "降雨",
    "drought": "日照", "dry skin": "干燥皮肤", "early bird": "早起", "effect spore": "孢子",
    "filter": "过滤", "flame body": "火焰之躯", "flash fire": "引火", "flower gift": "花之礼",
    "forecast": "阴晴不定", "forewarn": "预知梦", "friend guard": "友情防守",
    "frisk": "察觉", "gluttony": "贪吃鬼", "guts": "毅力", "harvest": "收获",
    "hustle": "活力", "hydration": "湿润之躯", "hyper cutter": "怪力钳",
    "ice body": "冰冻之躯", "illuminate": "发光", "immunity": "免疫",
    "inner focus": "精神力", "insomnia": "不眠", "intimidate": "威吓",
    "iron fist": "铁拳", "keen eye": "锐利目光", "levitate": "飘浮",
    "lightning rod": "避雷针", "limber": "柔软", "liquid voice": "湿润之声",
    "magic guard": "魔法防守", "magma armor": "熔岩铠甲", "marvel scale": "神奇鳞片",
    "minus": "负电", "mold breaker": "破格", "motor drive": "电气引擎",
    "moxie": "自信过度", "multiscale": "多重鳞片", "natural cure": "自然回复",
    "no guard": "无防守", "oblivious": "迟钝", "overcoat": "防尘",
    "overgrow": "茂盛", "own tempo": "我行我素", "pickpocket": "顺手牵羊",
    "pickup": "捡拾", "plus": "正电", "poison heal": "毒疗", "poison point": "毒刺",
    "pressure": "压迫感", "prankster": "恶作剧之心", "pure power": "瑜伽之力",
    "quick feet": "飞毛腿", "rain dish": "雨盘", "reckless": "舍身", "regenerator": "再生力",
    "rock head": "坚硬脑袋", "rough skin": "粗糙皮肤", "run away": "逃跑",
    "sand force": "沙之力", "sand rush": "拨沙", "sand stream": "扬沙",
    "sand veil": "沙隐", "scrappy": "胆量", "serene grace": "天恩", "shadow tag": "踩影",
    "shed skin": "蜕皮", "sheer force": "强行", "shell armor": "硬壳盔甲",
    "shield dust": "鳞粉", "simple": "单纯", "skill link": "连续攻击",
    "slow start": "慢启动", "snow cloak": "雪隐", "snow warning": "降雪",
    "solar power": "太阳之力", "solid rock": "坚硬岩石", "soundproof": "隔音",
    "speed boost": "加速", "static": "静电", "steadfast": "不屈之心",
    "stench": "恶臭", "sticky hold": "黏着", "storm drain": "引水",
    "sturdy": "结实", "suction cups": "吸盘", "super luck": "超幸运",
    "swarm": "虫之预感", "swift swim": "悠游自如", "synchronize": "同步",
    "technician": "技术高手", "thick fat": "厚脂肪", "torrent": "激流",
    "tough claws": "硬爪", "trace": "复制", "unaware": "纯朴", "unnerve": "紧张感",
    "vital spirit": "干劲", "volt absorb": "蓄电", "water absorb": "储水",
    "water veil": "水幕", "weak armor": "碎裂铠甲", "white smoke": "白色烟雾",
    "wonder guard": "神奇守护", "wonder skin": "奇迹皮肤", "zen mode": "达摩模式",
}


def localized_ability_name(ability_value=None, ability_id=None):
    """Prefer PKHeX's Simplified-Chinese ability catalog; never leak English UI labels."""
    resolved = ""
    if ability_id not in (None, ""):
        try:
            resolved = _pkhex_name_by_id("abilities", ability_id) or ""
        except Exception:
            resolved = ""

    candidates = []
    if isinstance(resolved, str) and resolved.strip():
        candidates.append(resolved.strip())
    if isinstance(ability_value, dict):
        ability_value = ability_value.get("name") or ability_value.get("label")
    if isinstance(ability_value, str) and ability_value.strip() and not ability_value.strip().isdigit():
        candidates.append(ability_value.strip())

    for candidate in candidates:
        if re.search(r"[\u3400-\u9fff]", candidate):
            return candidate
    for candidate in candidates:
        translated = ABILITY_EN_TO_ZH.get(candidate.casefold())
        if translated:
            return translated
    return "—"


def record_details(path):
    """Read Chinese summary fields plus Poké Ball and IVs from a captured PA8."""
    path = Path(path)
    result = {
        "species": path.stem, "level": "?", "gender": "未知", "nature": "未知",
        "species_id": None, "ball": "未知球", "ivs": None, "extra": {},
        "trainer_id": None, "secret_id": None, "ability": "—", "size": "—", "moves": [],
    }
    match = re.match(r"(?:\d{2}_)?(?P<id>\d{4})_(?P<species>.+?)_lv(?P<level>\d+)(?:_|$)", path.name, re.I)
    if match:
        result.update(
            species=match.group("species"),
            level=match.group("level"),
            species_id=int(match.group("id")),
        )
    owner = {}
    merged = {}
    try:
        from pokeldn import pokemon as pokemon_service
        raw = path.read_bytes()
        info = pokemon_service.SERVICE.check_bytes("pla", raw)
        merged = {**info, **(info.get("canonical") or {})}
        owner = pokemon_service.SERVICE.owner("pla", raw)
        species = merged.get("species") or owner.get("species")
        if isinstance(species, str) and species.strip():
            result["species"] = species
        level = merged.get("level")
        if level not in (None, ""):
            result["level"] = str(level)
        nature = merged.get("nature")
        if isinstance(nature, int) and 0 <= nature < len(NATURE_NAMES):
            result["nature"] = NATURE_NAMES[nature]
        elif isinstance(nature, str) and nature.strip():
            result["nature"] = NATURE_EN_TO_ZH.get(nature.strip(), nature)
        gender = owner.get("gender")
        if gender is None:
            gender = merged.get("gender")
        if gender in (0, "0", "M", "Male", "male"):
            result["gender"] = "♂"
        elif gender in (1, "1", "F", "Female", "female"):
            result["gender"] = "♀"
        elif gender in (2, "2", "Genderless", "genderless"):
            result["gender"] = "无性别"
        elif isinstance(gender, str) and gender.strip():
            result["gender"] = gender
        result["shiny"] = bool(merged.get("shiny", owner.get("shiny", False)))
        result["alpha"] = bool(merged.get("alpha", False))
        if result["species_id"] is None:
            species_id = merged.get("species_id") or merged.get("speciesId")
            if isinstance(species_id, int):
                result["species_id"] = species_id
        # Keep only common extra fields; the full service result is not shown in the main card.
        for key in ("ability", "item", "held_item", "moves", "evs", "effort_values", "height", "weight"):
            if merged.get(key) not in (None, "", [], {}):
                result["extra"][key] = merged[key]
    except Exception as exc:
        warning_key = ("pkhex", str(path))
        if warning_key not in _DETAIL_PARSE_WARNINGS:
            _DETAIL_PARSE_WARNINGS.add(warning_key)
            print(f"[Seed 信息解析] 无法读取 {path.name} 的完整宝可梦信息，将使用记录文件名和可解码字段：{type(exc).__name__}: {exc}", flush=True)
    raw_summary = _read_pa8_summary(path)
    for key, value in raw_summary.items():
        if key == "extra" and isinstance(value, dict):
            result.setdefault("extra", {}).update(value)
        elif value not in (None, {}, "未知球"):
            result[key] = value
    if result.get("ball") in (None, ""):
        result["ball"] = raw_summary.get("ball", "未知球")

    # If the PA8 decryptor is unavailable, recover the IDs from PKHeX's owner summary.
    if result.get("trainer_id") is None and isinstance(owner, dict):
        try:
            tid16 = int(owner.get("tid"))
            sid16 = int(owner.get("sid"))
            id32 = (sid16 << 16) | tid16
            result["trainer_id"] = id32 % 1_000_000
            result["secret_id"] = id32 // 1_000_000
        except (TypeError, ValueError):
            pass

    # Resolve the ability ID through PKHeX first. Some summary APIs return English labels
    # even when the UI language is Chinese, so prefer a localized catalog result/fallback.
    extra = result.get("extra") or {}
    ability_value = extra.get("ability")
    ability_id = result.get("ability_id")
    if ability_id in (None, ""):
        ability_id = extra.get("特性编号")
    result["ability"] = localized_ability_name(ability_value, ability_id)

    move_ids = result.get("move_ids") or extra.get("招式编号") or []
    move_names = [_pkhex_name_by_id("moves", move_id) for move_id in move_ids if int(move_id or 0) > 0]
    if not move_names:
        merged_moves = extra.get("moves")
        if isinstance(merged_moves, (list, tuple)):
            for move in merged_moves:
                if isinstance(move, dict):
                    name = move.get("name") or move.get("label")
                elif isinstance(move, str) and not move.strip().isdigit():
                    name = move.strip()
                else:
                    name = ""
                if name:
                    move_names.append(str(name))
    result["moves"] = move_names[:4]
    result["size"] = result.get("size") or "—"
    result["trainer_id"] = result.get("trainer_id")
    result["secret_id"] = result.get("secret_id")
    result["species_en"] = result["species"]
    result["species"] = chinese_species_name(result["species"], result.get("species_id"))
    return result


# Simplified-Chinese names for common Legends: Arceus species.
# If a name is not in this table, the original PKHeX name is kept.
CHINESE_SPECIES = {
    "Rowlet": "木木枭", "Dartrix": "投羽枭", "Decidueye": "狙射树枭",
    "Cyndaquil": "火球鼠", "Quilava": "火岩鼠", "Typhlosion": "火暴兽",
    "Oshawott": "水水獭", "Dewott": "双刃丸", "Samurott": "大剑鬼",
    "Starly": "姆克儿", "Staravia": "姆克鸟", "Staraptor": "姆克鹰",
    "Bidoof": "大牙狸", "Bibarel": "大尾狸", "Shinx": "小猫怪",
    "Luxio": "勒克猫", "Luxray": "伦琴猫", "Wurmple": "刺尾虫",
    "Silcoon": "甲壳茧", "Beautifly": "狩猎凤蝶", "Cascoon": "盾甲茧",
    "Dustox": "毒粉蝶", "Ponyta": "小火马", "Rapidash": "烈焰马",
    "Eevee": "伊布", "Vaporeon": "水伊布", "Jolteon": "雷伊布",
    "Flareon": "火伊布", "Espeon": "太阳伊布", "Umbreon": "月亮伊布",
    "Leafeon": "叶伊布", "Glaceon": "冰伊布", "Sylveon": "仙子伊布",
    "Zubat": "超音蝠", "Golbat": "大嘴蝠", "Crobat": "叉字蝠",
    "Geodude": "小拳石", "Graveler": "隆隆石", "Golem": "隆隆岩",
    "Psyduck": "可达鸭", "Golduck": "哥达鸭", "Abra": "凯西",
    "Kadabra": "勇基拉", "Alakazam": "胡地", "Machop": "腕力",
    "Machoke": "豪力", "Machamp": "怪力", "Budew": "含羞苞",
    "Roselia": "毒蔷薇", "Roserade": "罗丝雷朵", "Cherubi": "樱花宝",
    "Cherrim": "樱花儿", "Paras": "派拉斯", "Parasect": "派拉斯特",
    "Drifloon": "飘飘球", "Drifblim": "附和气球", "Buizel": "泳气鼬",
    "Floatzel": "浮潜鼬", "Burmy": "结草儿", "Wormadam": "结草贵妇",
    "Mothim": "绅士蛾", "Combee": "三蜜蜂", "Vespiquen": "蜂女王",
    "Pachirisu": "帕奇利兹", "Aipom": "长尾怪手", "Ambipom": "双尾怪手",
    "Magikarp": "鲤鱼王", "Gyarados": "暴鲤龙", "Shellos": "无壳海兔",
    "Gastrodon": "海兔兽", "Stunky": "臭鼬噗", "Skuntank": "坦克臭鼬",
    "Bronzor": "铜镜怪", "Bronzong": "青铜钟", "Bonsly": "盆才怪",
    "Mime Jr.": "魔尼尼", "Happiny": "小福蛋", "Chansey": "吉利蛋",
    "Blissey": "幸福蛋", "Riolu": "利欧路", "Lucario": "路卡利欧",
    "Teddiursa": "熊宝宝", "Ursaring": "圈圈熊", "Ursaluna": "月月熊",
    "Goomy": "黏黏宝", "Sliggoo": "黏美儿", "Goodra": "黏美龙",
    "Onix": "大岩蛇", "Steelix": "大钢蛇", "Rhyhorn": "独角犀牛",
    "Rhydon": "钻角犀兽", "Rhyperior": "超甲狂犀", "Bonsly": "盆才怪",
    "Tangela": "蔓藤怪", "Tangrowth": "巨蔓藤", "Yanma": "蜻蜻蜓",
    "Yanmega": "远古巨蜓", "Hippopotas": "沙河马", "Hippowdon": "河马兽",
    "Skorupi": "钳尾蝎", "Drapion": "龙王蝎", "Croagunk": "不良蛙",
    "Toxicroak": "毒骷蛙", "Ralts": "拉鲁拉丝", "Kirlia": "奇鲁莉安",
    "Gardevoir": "沙奈朵", "Gallade": "艾路雷朵", "Nosepass": "朝北鼻",
    "Probopass": "大朝北鼻", "Duskull": "夜巡灵", "Dusclops": "彷徨夜灵",
    "Dusknoir": "黑夜魔灵", "Snorunt": "雪童子", "Glalie": "冰鬼护",
    "Froslass": "雪妖女", "Swinub": "小山猪", "Piloswine": "长毛猪",
    "Mamoswine": "象牙猪", "Bergmite": "冰宝", "Avalugg": "冰岩怪",
    "Spheal": "海豹球", "Sealeo": "海魔狮", "Walrein": "帝牙海狮",
    "Remoraid": "铁炮鱼", "Octillery": "章鱼桶", "Mantyke": "小球飞鱼",
    "Mantine": "巨翅飞鱼", "Qwilfish": "千针鱼", "Overqwil": "万针鱼",
    "Sneasel": "狃拉", "Weavile": "玛狃拉", "Sneasler": "大狃拉",
    "Murkrow": "黑暗鸦", "Honchkrow": "乌鸦头头", "Gligar": "天蝎",
    "Gliscor": "天蝎王", "Cleffa": "皮宝宝", "Clefairy": "皮皮",
    "Clefable": "皮可西", "Togepi": "波克比", "Togetic": "波克基古",
    "Togekiss": "波克基斯", "Porygon": "多边兽", "Porygon2": "多边兽Ⅱ",
    "Porygon-Z": "多边兽Ｚ", "Misdreavus": "梦妖", "Mismagius": "梦妖魔",
    "Spiritomb": "花岩怪", "Gible": "圆陆鲨", "Gabite": "尖牙陆鲨",
    "Garchomp": "烈咬陆鲨", "Nosepass": "朝北鼻", "Voltorb": "霹雳电球",
    "Electrode": "顽皮雷弹", "Growlithe": "卡蒂狗", "Arcanine": "风速狗",
    "Basculin": "野蛮鲈鱼", "Basculegion": "幽尾玄鱼", "Wyrdeer": "诡角鹿",
    "Kleavor": "劈斧螳螂", "Enamorus": "眷恋云", "Tornadus": "龙卷云",
    "Thundurus": "雷电云", "Landorus": "土地云", "Shaymin": "谢米",
    "Darkrai": "达克莱伊", "Cresselia": "克雷色利亚", "Heatran": "席多蓝恩",
    "Regigigas": "雷吉奇卡斯", "Dialga": "帝牙卢卡", "Palkia": "帕路奇亚",
    "Giratina": "骑拉帝纳", "Uxie": "由克希", "Mesprit": "艾姆利多",
    "Azelf": "亚克诺姆", "Arceus": "阿尔宙斯",
}
NATURE_EN_TO_ZH = {
    "Hardy": "勤奋", "Lonely": "怕寂寞", "Brave": "勇敢", "Adamant": "固执",
    "Naughty": "顽皮", "Bold": "大胆", "Docile": "坦率", "Relaxed": "悠闲",
    "Impish": "淘气", "Lax": "乐天", "Timid": "胆小", "Hasty": "急躁",
    "Serious": "认真", "Jolly": "爽朗", "Naive": "天真", "Modest": "内敛",
    "Mild": "慢吞吞", "Quiet": "冷静", "Bashful": "害羞", "Rash": "马虎",
    "Calm": "温和", "Gentle": "温顺", "Sassy": "自大", "Careful": "慎重",
    "Quirky": "浮躁",
}



# Extra Chinese names for additional Hisui MMO species and common missing entries.
CHINESE_SPECIES.update({
    "Pichu": "皮丘", "Pikachu": "皮卡丘", "Raichu": "雷丘",
    "Piplup": "波加曼", "Prinplup": "波皇子", "Empoleon": "帝王拿波",
    "Turtwig": "草苗龟", "Grotle": "树林龟", "Torterra": "土台龟",
    "Chimchar": "小火焰猴", "Monferno": "猛火猴", "Infernape": "烈焰猴",
    "Carnivine": "尖牙笼", "Munchlax": "小卡比兽", "Snorlax": "卡比兽",
    "Zorua": "索罗亚", "Zoroark": "索罗亚克",
    "Rufflet": "毛头小鹰", "Braviary": "勇士雄鹰",
    "Petilil": "百合根娃娃", "Lilligant": "裙儿小姐",
    "Buneary": "卷卷耳", "Lopunny": "长耳兔",
    "Chingling": "铃铛响", "Chimecho": "风铃铃",
    "Finneon": "荧光鱼", "Lumineon": "霓虹鱼",
    "Magnemite": "小磁怪", "Magneton": "三合一磁怪", "Magnezone": "自爆磁怪",
    "Elekid": "电击怪", "Electabuzz": "电击兽", "Electivire": "电击魔兽",
    "Magby": "鸭嘴宝宝", "Magmar": "鸭嘴火兽", "Magmortar": "鸭嘴炎兽",
    "Scyther": "飞天螳螂", "Scizor": "巨钳螳螂", "Heracross": "赫拉克罗斯",
    "Lickitung": "大舌头", "Lickilicky": "大舌舔",
    "Rotom": "洛托姆", "Snover": "雪笠怪", "Abomasnow": "暴雪王",
    "Cranidos": "头盖龙", "Rampardos": "战槌龙",
    "Shieldon": "盾甲龙", "Bastiodon": "护城龙",
    "Barboach": "泥泥鳅", "Whiscash": "鲶鱼王",
    "Stantler": "惊角鹿", "Ninetales": "九尾", "Vulpix": "六尾",
    "Dugtrio": "三地鼠", "Diglett": "地鼠",
    "Sealeo": "海魔狮", "Walrein": "帝牙海狮",
    "Machop": "腕力", "Sneasel": "狃拉",
    "Qwilfish": "千针鱼", "Overqwil": "万针鱼",
})

def chinese_species_name(name, species_id=None):
    """Prefer Pokeldn's localized PKHeX species catalog, then use the bundled CN map."""
    global _SPECIES_NAME_CACHE
    cleaned = str(name or "").strip()
    if species_id is not None:
        try:
            if _SPECIES_NAME_CACHE is None:
                _SPECIES_NAME_CACHE = {}
                from pokeldn import pokemon as pokemon_service
                entries = pokemon_service.SERVICE.names("pla", "species")
                if isinstance(entries, dict):
                    normalized_entries = []
                    for key, value in entries.items():
                        if isinstance(value, dict):
                            normalized_entries.append(value)
                        else:
                            normalized_entries.append({"id": key, "name": value})
                    entries = normalized_entries
                for entry in entries or ():
                    if not isinstance(entry, dict):
                        continue
                    sid = entry.get("id", entry.get("species_id"))
                    label = entry.get("name")
                    if sid is None or not isinstance(label, str) or not label.strip():
                        continue
                    try:
                        _SPECIES_NAME_CACHE[int(sid)] = label.strip()
                    except (TypeError, ValueError):
                        continue
            localized = _SPECIES_NAME_CACHE.get(int(species_id)) if _SPECIES_NAME_CACHE else None
            if localized and any("\u3400" <= ch <= "\u9fff" for ch in localized):
                return localized
        except Exception:
            # Older Pokeldn builds may not expose localized species names.
            pass
    if not cleaned:
        return "未知宝可梦"
    return CHINESE_SPECIES.get(cleaned, cleaned)


def open_seed_text_file(path):
    """Open the plain-text Seed file in the system's default text editor."""
    path = Path(path)
    try:
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        print(f"[Seed 文件] 已尝试打开 Seed 文本文件：{path}", flush=True)
        print("[Seed 文件] 文件中只有最终群体 Seed，可在文本编辑器中选中并复制。", flush=True)
        return True
    except Exception as exc:
        print(f"[Seed 文件] 无法自动打开文本文件：{exc}", flush=True)
        print(f"[Seed 文件] 请手动打开并复制：{path}", flush=True)
        return False




_SPRITE_CACHE = {}
_SPRITE_MISSING_LOGGED = set()
_SPRITE_DOWNLOAD_ATTEMPTED = set()
_SPRITE_DOWNLOAD_IN_PROGRESS = set()
_SPRITE_TASK_LOCK = threading.RLock()
_PANEL_RENDER_LOCK = threading.RLock()
_PANEL_WARNING_LOGGED = set()
_PANEL_LAST_PAYLOADS = {}
_PANEL_INITIALIZED = False
_DETAILS_FILE_RECORD_SIGNATURE = None
_DETAILS_FILE_FINAL_PHASE = None
_SPECIES_NAME_CACHE = None
_PKHEX_NAME_CACHE = {}
_DETAIL_PARSE_WARNINGS = set()


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_PANEL_IMAGE_BYTES = 2 * 1024 * 1024
_SPRITE_DOWNLOAD_TIMEOUT = 2.0
_SPRITE_URLS = (
    "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/versions/generation-viii/legends-arceus/icons/{id}.png",
    "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{id}.png",
)


def _supported_image_bytes(data):
    """Return the extension for a structurally valid, small PNG/JPEG, else None."""
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > _MAX_PANEL_IMAGE_BYTES:
        return None
    data = bytes(data)
    if _valid_png_bytes(data):
        return ".png"
    if _valid_jpeg_bytes(data):
        return ".jpg"
    return None


def _valid_png_bytes(data):
    """Check the PNG signature, IHDR dimensions, and terminal IEND chunk without Pillow."""
    if not data.startswith(_PNG_SIGNATURE) or len(data) < 45:
        return False
    if int.from_bytes(data[8:12], "big") != 13 or data[12:16] != b"IHDR":
        return False
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if not (0 < width <= 8192 and 0 < height <= 8192):
        return False
    return data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")


def _valid_jpeg_bytes(data):
    """Check JPEG start/end markers without needing an image-processing dependency."""
    return len(data) >= 4 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")


def _supported_image_file(path):
    """Validate image structure; do not pass WebP/SVG/HTML/truncated files to panel.show."""
    try:
        path = Path(path)
        if not path.is_file() or not (0 < path.stat().st_size <= _MAX_PANEL_IMAGE_BYTES):
            return False
        suffix = path.suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg"):
            return False
        data = path.read_bytes()
        if suffix == ".png":
            return _valid_png_bytes(data)
        return _valid_jpeg_bytes(data)
    except OSError:
        return False


def _prune_sprite_files(folder, keep=128):
    """Bound the plugin's icon caches so collecting many species cannot grow them forever."""
    try:
        folder = Path(folder)
        candidates = [
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg")
            and _supported_image_file(path)
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for stale in candidates[max(0, int(keep)):]:
            try:
                stale.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _sprite_search_roots():
    """Known Pokeldn sprite/cache locations plus the plugin's own persistent cache."""
    roots = []
    try:
        from pokeldn.app.paths import DATA, ROOT
        data_root = Path(DATA)
        app_root = Path(ROOT)
        roots.extend((
            data_root / "sprites",
            data_root / "pokemon" / "sprites",
            data_root / "cache" / "sprites",
            app_root / "sprites",
            app_root / "assets" / "sprites",
            app_root / "assets" / "pokemon",
            app_root / "resources" / "sprites",
            app_root / "resources" / "pokemon",
            app_root / "static" / "sprites",
        ))
    except Exception:
        pass
    try:
        data_root = Path(plugin_data())
        roots.extend((data_root / "sprite-cache", data_root / "panel-sprites"))
    except Exception:
        pass
    roots.append(Path(__file__).resolve().parent / "sprites")

    unique = []
    seen = set()
    for root in roots:
        try:
            root = Path(root)
            key = str(root.resolve()).casefold()
            if key not in seen and root.is_dir():
                seen.add(key)
                unique.append(root)
        except (OSError, RuntimeError):
            continue
    return unique


def find_cached_sprite(detail):
    """Find an existing local PNG/JPEG sprite using bounded path checks only.

    Never recursively walk the application install tree while the collection monitor is
    running: a large sprite directory can block record collection and leave the UI stale.
    Online fallback is scheduled separately by _panel_icon_path.
    """
    if not isinstance(detail, dict):
        return None
    sid = detail.get("species_id")
    english_name = str(detail.get("species_en") or detail.get("species") or "")
    try:
        numeric_id = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        numeric_id = None
    roots = _sprite_search_roots()
    cache_key = (tuple(str(root) for root in roots), str(numeric_id), english_name.lower())
    if cache_key in _SPRITE_CACHE:
        cached = _SPRITE_CACHE[cache_key]
        if cached and _supported_image_file(cached):
            return Path(cached)
        if cached is not None:
            _SPRITE_CACHE.pop(cache_key, None)

    stems = []
    if numeric_id is not None and numeric_id > 0:
        stems.extend((str(numeric_id), f"{numeric_id:03d}", f"{numeric_id:04d}", f"pokemon-{numeric_id}"))
    slug = re.sub(r"[^a-z0-9]+", "-", english_name.lower()).strip("-")
    stems = list(dict.fromkeys(stem for stem in stems if stem))
    if numeric_id is not None and slug:
        stems.append(f"{numeric_id}-{slug}")
    if slug:
        stems.extend((slug, f"{slug}-icon"))
    stems = list(dict.fromkeys(stems))
    exts = ("png", "jpg", "jpeg")

    # Case-insensitive flat-cache lookup also finds files created by v0.3.6 on
    # case-sensitive filesystems (for example ``395-Empoleon.png``).
    if numeric_id is not None:
        prefix = f"{numeric_id}-".casefold()
        for root in roots:
            try:
                for candidate in root.iterdir():
                    if (candidate.is_file() and candidate.suffix.lower() in (".png", ".jpg", ".jpeg")
                            and candidate.name.casefold().startswith(prefix)
                            and (not slug or slug in candidate.stem.casefold())
                            and _supported_image_file(candidate)):
                        _SPRITE_CACHE[cache_key] = candidate
                        return candidate
            except OSError:
                continue

    for root in roots:
        candidates = []
        for stem in stems:
            for ext in exts:
                candidates.extend((
                    root / f"{stem}.{ext}",
                    root / "pokemon" / f"{stem}.{ext}",
                    root / "icons" / f"{stem}.{ext}",
                    root / "pokemon" / "other" / "official-artwork" / f"{stem}.{ext}",
                    root / "pokemon" / "other" / "home" / f"{stem}.{ext}",
                    root / "pokemon" / "versions" / "generation-viii" / "legends-arceus" / "icons" / f"{stem}.{ext}",
                    root / "versions" / "generation-viii" / "legends-arceus" / "icons" / f"{stem}.{ext}",
                ))
        for candidate in candidates:
            if _supported_image_file(candidate):
                _SPRITE_CACHE[cache_key] = candidate
                return candidate

    # Do not cache misses: Pokeldn may add a local image later in the same session.
    return None


def download_sprite(detail):
    """Fetch a small PLA icon, then a standard sprite, using only Python's standard library.

    Downloaded files are cached in plugin-data/sprite-cache. No Pillow or image conversion
    is required: panel.show accepts PNG/JPEG, and the source URLs provide PNG files.
    """
    if not isinstance(detail, dict):
        return None
    try:
        sid = int(detail.get("species_id"))
    except (TypeError, ValueError):
        return None
    if sid <= 0 or sid > 2000:
        return None

    english_name = str(detail.get("species_en") or detail.get("species") or "pokemon")
    # Lowercase filenames so the same cache works on Windows and case-sensitive systems.
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", english_name).strip("-").lower()[:48] or "pokemon"
    try:
        cache_root = Path(plugin_data()) / "sprite-cache"
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        key = ("download-cache", str(exc))
        if key not in _SPRITE_MISSING_LOGGED:
            _SPRITE_MISSING_LOGGED.add(key)
            print(f"[Seed 头像] 无法创建图标缓存目录：{exc}", flush=True)
        return None

    for ext in (".png", ".jpg", ".jpeg"):
        cached = cache_root / f"{sid}-{slug}{ext}"
        if _supported_image_file(cached):
            return cached

    attempt_key = (sid, slug.lower())
    with _SPRITE_TASK_LOCK:
        if attempt_key in _SPRITE_DOWNLOAD_ATTEMPTED:
            return None
        _SPRITE_DOWNLOAD_ATTEMPTED.add(attempt_key)

    for template in _SPRITE_URLS:
        url = template.format(id=sid)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Pokeldn-PLA-MMO-Seed-Plugin/0.3.11", "Accept": "image/png,image/jpeg"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_SPRITE_DOWNLOAD_TIMEOUT) as response:
                data = response.read(_MAX_PANEL_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # A missing PLA-specific icon is expected for some forms; try the generic sprite.
            if exc.code in (403, 404, 410):
                continue
            print(f"[Seed 头像] 在线图标请求失败（HTTP {exc.code}），改用占位图。", flush=True)
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Both URLs use the same host; a connection/DNS failure will affect the fallback too.
            key = ("download-network", type(exc).__name__)
            if key not in _SPRITE_MISSING_LOGGED:
                _SPRITE_MISSING_LOGGED.add(key)
                print(f"[Seed 头像] 无法连接在线图标源，改用占位图：{type(exc).__name__}", flush=True)
            break
        except Exception as exc:
            key = ("download-unexpected", type(exc).__name__, str(exc))
            if key not in _SPRITE_MISSING_LOGGED:
                _SPRITE_MISSING_LOGGED.add(key)
                print(f"[Seed 头像] 在线图标获取失败，改用占位图：{type(exc).__name__}: {exc}", flush=True)
            break

        ext = _supported_image_bytes(data)
        if not ext:
            # An unavailable/unsupported PLA-specific asset may still have a generic PNG.
            continue
        target = cache_root / f"{sid}-{slug}{ext}"
        temp = target.with_name(target.name + ".tmp")
        try:
            temp.write_bytes(data)
            temp.replace(target)
            if _supported_image_file(target):
                _SPRITE_CACHE.clear()  # a fresh downloaded sprite may satisfy a later local lookup
                _prune_sprite_files(cache_root, keep=128)
                print(f"[Seed 头像] 已下载并缓存 {english_name} 图标。", flush=True)
                return target
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            key = ("download-write", str(exc))
            if key not in _SPRITE_MISSING_LOGGED:
                _SPRITE_MISSING_LOGGED.add(key)
                print(f"[Seed 头像] 无法保存下载的图标：{exc}", flush=True)
            return None

    key = ("download-miss", sid, slug.lower())
    if key not in _SPRITE_MISSING_LOGGED:
        _SPRITE_MISSING_LOGGED.add(key)
        print(f"[Seed 头像] 本地缓存和在线图标源均未找到 {english_name}；此卡将显示占位图。", flush=True)
    return None


def _schedule_sprite_download(detail):
    """Start a best-effort icon download without blocking collection or panel rendering."""
    if not isinstance(detail, dict):
        return False
    try:
        sid = int(detail.get("species_id"))
    except (TypeError, ValueError):
        return False
    if sid <= 0 or sid > 2000:
        return False
    english_name = str(detail.get("species_en") or detail.get("species") or "pokemon")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", english_name).strip("-").lower()[:48] or "pokemon"
    key = (sid, slug)
    with _SPRITE_TASK_LOCK:
        if key in _SPRITE_DOWNLOAD_ATTEMPTED or key in _SPRITE_DOWNLOAD_IN_PROGRESS:
            return False
        _SPRITE_DOWNLOAD_IN_PROGRESS.add(key)

    def worker():
        try:
            download_sprite(dict(detail))
        except Exception as exc:
            warning_key = ("async-sprite-download", sid, type(exc).__name__)
            if warning_key not in _SPRITE_MISSING_LOGGED:
                _SPRITE_MISSING_LOGGED.add(warning_key)
                print(f"[Seed 头像] 后台下载图标失败，继续使用占位图：{type(exc).__name__}", flush=True)
        finally:
            with _SPRITE_TASK_LOCK:
                _SPRITE_DOWNLOAD_IN_PROGRESS.discard(key)

    try:
        threading.Thread(target=worker, name=f"pla-sprite-{sid}", daemon=True).start()
        return True
    except Exception:
        with _SPRITE_TASK_LOCK:
            _SPRITE_DOWNLOAD_IN_PROGRESS.discard(key)
        return False

def ensure_placeholder_image():
    """Copy the bundled placeholder into the app data folder for panel.show(image=...)."""
    try:
        target = Path(plugin_data()) / "pokemon-placeholder.png"
        if target.is_file() and 0 < target.stat().st_size <= 2 * 1024 * 1024:
            return target
        source = Path(__file__).resolve().parent / "pokemon-placeholder.png"
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if target.is_file():
                return target
    except Exception as exc:
        key = ("placeholder", str(exc))
        if key not in _PANEL_WARNING_LOGGED:
            _PANEL_WARNING_LOGGED.add(key)
            print(f"[Seed 头像] 无法准备占位图：{exc}", flush=True)
    return None




def _fallback_result_lines(details, count, seed=None, phase="收录中", status=""):
    """Build a complete, native-text result card (max 12 lines) if image rendering fails."""
    phase_labels = {
        "收录中": "收录中", "结束联机中": "正常结束联机中", "计算中": "正在反推 Seed",
        "已完成": "计算成功", "计算失败": "计算失败", "缺少计算程序": "缺少计算程序",
        "采集失败": "采集失败", "准备就绪": "准备就绪",
    }
    short_status = re.sub(r"\s+", " ", str(status or "")).strip()
    if len(short_status) > 120:
        short_status = short_status[:117] + "…"
    phase_label = phase_labels.get(phase, phase)
    if phase == "已完成" and seed:
        status_line = "状态\t✓ 计算成功"
    elif short_status and short_status != phase_label:
        status_line = f"状态\t{phase_label}：{short_status}"
    else:
        status_line = f"状态\t{phase_label}"
    lines = [status_line]
    if seed:
        lines.append(f"群体 Seed\t{seed}")
    elif phase in ("计算失败", "采集失败", "缺少计算程序"):
        lines.append("最终群体 Seed\t未生成")
    else:
        lines.append(f"收录进度\t{count}/4")
    lines.append(f"本次参与计算的宝可梦\t{count}/4")
    lines.append("个体值顺序\tHP / 攻击 / 防御 / 特攻 / 特防 / 速度")
    by_index = {i: details[i] for i in range(min(len(details), count))}
    for i in range(4):
        detail = by_index.get(i)
        if detail:
            species = str(detail.get("species") or "未知宝可梦")
            level = str(detail.get("level") or "?")
            gender = str(detail.get("gender") or "未知")
            nature = str(detail.get("nature") or "未知")
            ball = str(detail.get("ball") or "未知球")
            ivs = detail.get("ivs")
            iv_text = "/".join(str(v) for v in ivs) if isinstance(ivs, (list, tuple)) and len(ivs) == 6 else "暂不可用"
            lines.append(f"{i + 1:02d} {species} Lv.{level}\t{gender} · {nature} · {ball}")
            lines.append(f"个体值\t{iv_text}")
        else:
            lines.append(f"{i + 1:02d} 宝可梦\t等待收录")
            lines.append("信息\t选择宝可梦后停留在交换确认页面")
    return [str(line)[:200] for line in lines[:12]]


def _panel_icon_path(detail=None):
    """Return a local image inside this plugin's data folder for one card thumbnail.

    Look up local Pokeldn assets first. If absent, schedule a best-effort background
    download and return the placeholder immediately so capture monitoring never waits
    on HTTP. The next native UI refresh will pick up the cached image.
    """
    try:
        placeholder = ensure_placeholder_image()
        source = find_cached_sprite(detail) if detail else None
        if source is None and detail:
            _schedule_sprite_download(detail)
        if source:
            try:
                source = Path(source)
                size = source.stat().st_size
                if 0 < size <= _MAX_PANEL_IMAGE_BYTES and _supported_image_file(source):
                    data_root = Path(plugin_data())
                    sprite_root = data_root / "panel-sprites"
                    sprite_root.mkdir(parents=True, exist_ok=True)
                    sid = (detail or {}).get("species_id")
                    slug_source = str((detail or {}).get("species_en") or (detail or {}).get("species") or "pokemon")
                    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug_source).strip("-")[:48] or "pokemon"
                    identity = str(sid) if sid is not None else slug
                    ext = source.suffix.lower()
                    if ext not in (".png", ".jpg", ".jpeg"):
                        ext = ".png"
                    target = sprite_root / f"{identity}-{slug}{ext}"
                    if (not _supported_image_file(target)) or target.stat().st_size != size:
                        shutil.copyfile(source, target)
                    if _supported_image_file(target):
                        try:
                            target.touch()
                        except OSError:
                            pass
                        _prune_sprite_files(sprite_root, keep=128)
                        return str(target)
            except Exception as exc:
                warning_key = ("panel-sprite-copy", str(source), type(exc).__name__, str(exc))
                if warning_key not in _PANEL_WARNING_LOGGED:
                    _PANEL_WARNING_LOGGED.add(warning_key)
                    print(f"[Seed 头像] 无法复制宝可梦图标到插件数据目录：{exc}", flush=True)
        if placeholder and Path(placeholder).is_file():
            return str(placeholder)
    except Exception as exc:
        warning_key = ("panel-image", type(exc).__name__, str(exc))
        if warning_key not in _PANEL_WARNING_LOGGED:
            _PANEL_WARNING_LOGGED.add(warning_key)
            print(f"[Seed 头像] 无法准备卡片图标：{exc}", flush=True)
    return None


def _summary_panel_payload(count, status, tone="info", seed=None, phase="收录中"):
    """Build a fixed-height native status card so progress updates do not shift the UI."""
    phase_labels = {
        "收录中": "收录中", "结束联机中": "正常结束联机中", "计算中": "正在反推 Seed",
        "已完成": "计算成功", "计算失败": "计算失败", "缺少计算程序": "缺少计算程序",
        "采集失败": "采集失败", "准备就绪": "准备就绪", "无法安全启动": "无法安全启动",
    }
    phase_label = phase_labels.get(phase, str(phase or "运行中"))
    raw_status = re.sub(r"\s+", " ", str(status or "")).strip()
    if phase == "已完成" and seed:
        status_text = "✓ 计算成功，已找到最终群体 Seed"
        panel_tone = "ok"
    elif phase in ("计算失败", "采集失败", "缺少计算程序", "无法安全启动"):
        status_text = raw_status or phase_label
        panel_tone = "error" if tone == "error" else "warning"
    elif phase == "结束联机中":
        status_text = "四只已收齐，正在正常结束交换会话"
        panel_tone = "ok"
    elif phase == "计算中":
        status_text = "四只已收齐，正在检查记录并反推 Seed"
        panel_tone = "info"
    elif phase == "收录中":
        status_text = f"正在收录宝可梦：{count}/4"
        panel_tone = "info" if count < 4 else "ok"
    else:
        status_text = raw_status or phase_label
        panel_tone = tone if tone in ("info", "ok", "warning", "error") else "info"

    status_text = status_text[:190]
    if seed:
        lines = [f"群体 Seed\t{str(seed)[:180]}", f"状态\t{status_text}"]
        chips = [("收录", f"{count}/4"), ("状态", "计算成功")]
    else:
        if phase == "收录中":
            hint = raw_status if raw_status and raw_status != status_text else "只在交换确认页面收录，不确认真正交换"
        elif phase == "结束联机中":
            hint = "已发起正常结束请求；Seed 计算随即开始"
        elif phase == "计算中":
            hint = "最终群体 Seed 将显示在此卡片"
        elif phase in ("计算失败", "采集失败", "缺少计算程序", "无法安全启动"):
            hint = "已保存的记录会保留，可查看右侧输出了解原因"
        else:
            hint = raw_status if raw_status and raw_status != status_text else phase_label
        lines = [f"状态\t{status_text}", f"提示\t{str(hint)[:180]}"]
        chips = [("收录", f"{count}/4"), ("阶段", phase_label[:32])]
    return lines[:12], chips[:6], panel_tone


def _panel_display_units(text):
    """Approximate native-card text width in half-width character units."""
    width = 0
    for char in str(text):
        if unicodedata.east_asian_width(char) in ("F", "W"):
            width += 2
        else:
            width += 1
    return width


def _panel_two_column_line(left, right, target_units=28):
    """Put two bold text groups on one native value row without adding label headings."""
    left = str(left or "—")
    right = str(right or "—")
    padding = max(1, (int(target_units) - _panel_display_units(left) + 1) // 2)
    return "\t" + left + ("\u3000" * padding) + right


def _pokemon_panel_payload(index, detail):
    """Return a compact card with two aligned information groups across three rows."""
    if detail:
        species = str(detail.get("species") or "未知宝可梦")
        level = str(detail.get("level") or "?")
        gender = str(detail.get("gender") or "未知")
        nature = str(detail.get("nature") or "未知")
        ball = str(detail.get("ball") or "未知球")
        ivs = detail.get("ivs")
        iv_text = "/".join(str(value) for value in ivs) if isinstance(ivs, (list, tuple)) and len(ivs) == 6 else "—"
        trainer_id = detail.get("trainer_id")
        secret_id = detail.get("secret_id")
        tid_sid = f"{int(trainer_id):06d} / {int(secret_id):04d}" if trainer_id is not None and secret_id is not None else "— / —"
        ability = str(detail.get("ability") or "—")
        size = str(detail.get("size") or "—")
        ability_size = " · ".join(value for value in (ability, size) if value and value != "—") or "—"
        moves = [str(move) for move in (detail.get("moves") or []) if move and str(move) != "—"][:4]
        moves += ["—"] * (4 - len(moves))
        title = f"{index:02d}  {species} Lv.{level}"[:80]
        # A leading tab keeps all detail text in Pokeldn's bright/bold value column.
        # Each row has a left and right group, aligned with full-width spacing. Three rows
        # replace the previous four-line stack while retaining every field and all four moves.
        lines = [
            _panel_two_column_line(f"{gender} · {nature} · {ball}", tid_sid),
            _panel_two_column_line(f"个体值 {iv_text}", ability_size),
            _panel_two_column_line(f"{moves[0]} · {moves[1]}", f"{moves[2]} · {moves[3]}"),
        ]
        chips = [("位置", f"{index}/4"), ("状态", "已收录")]
        return title, [line[:200] for line in lines], chips, "ok"

    # Keep a fixed three-row height before and after capture to avoid card jumping.
    title = f"{index:02d}  等待收录"
    lines = [
        _panel_two_column_line("等待进入交换确认页面", "— / —"),
        _panel_two_column_line("个体值 —", "—"),
        _panel_two_column_line("— · —", "— · —"),
    ]
    chips = [("位置", f"{index}/4"), ("状态", "待收录")]
    return title, lines, chips, "info"


def reset_collection_panel_state():
    """Reset render signatures once per tool run; existing cards are cleared on first render only."""
    global _PANEL_INITIALIZED, _DETAILS_FILE_RECORD_SIGNATURE, _DETAILS_FILE_FINAL_PHASE
    with _PANEL_RENDER_LOCK:
        _PANEL_INITIALIZED = False
        _PANEL_LAST_PAYLOADS.clear()
        _DETAILS_FILE_RECORD_SIGNATURE = None
        _DETAILS_FILE_FINAL_PHASE = None


def update_collection_ui(count, status, tone="info", records=None, seed=None, phase="收录中"):
    """Serialize native panel updates and skip unchanged cards to prevent flicker."""
    with _PANEL_RENDER_LOCK:
        return _update_collection_ui_locked(count, status, tone, records, seed, phase)


def _update_collection_ui_locked(count, status, tone="info", records=None, seed=None, phase="收录中"):
    """Render fixed status and Pokemon cards without blocking on destructive clears.

    Existing card IDs are stable and panel.show replaces their contents. Calling
    panel.clear synchronously before the first render can stall the whole collection
    process in some Pokeldn UI states, leaving only the tool settings visible. Never
    clear the active card IDs here; render/replace them directly instead.
    """
    global _PANEL_INITIALIZED, _DETAILS_FILE_RECORD_SIGNATURE, _DETAILS_FILE_FINAL_PHASE
    count = max(0, min(4, int(count)))
    records = list(records or [])[:4]
    details = []
    for path in records:
        try:
            details.append(record_details(path))
        except Exception as exc:
            details.append(None)
            warning_key = ("record-details-ui", str(path), type(exc).__name__, str(exc))
            if warning_key not in _PANEL_WARNING_LOGGED:
                _PANEL_WARNING_LOGGED.add(warning_key)
                print(f"[Seed 界面] 无法解析一只宝可梦的展示信息，仍继续绘制其他卡片：{exc}", flush=True)

    # Emit this before touching the UI API. If the host renderer stalls, the output
    # makes the exact boundary visible instead of looking like the plugin did nothing.
    print(f"[Seed 界面] 正在更新主状态卡和四张宝可梦卡（{count}/4）。", flush=True)
    try:
        from pokeldn import panel
    except Exception as exc:
        warning_key = ("panel-import", type(exc).__name__, str(exc))
        if warning_key not in _PANEL_WARNING_LOGGED:
            _PANEL_WARNING_LOGGED.add(warning_key)
            print(f"[Seed 界面] 无法加载原生卡片接口：{exc}", flush=True)
        for line in _fallback_result_lines(details, count, seed=seed, phase=phase, status=status):
            print(f"[Seed 结果] {line}", flush=True)
        panel = None

    render_errors = []
    if panel is not None:
        # Do not call panel.clear for IDs that are immediately re-used by panel.show.
        # Updating the same IDs replaces stale contents and avoids a destructive UI IPC
        # call that can block collection before the network host starts.
        summary_lines, summary_chips, summary_tone = _summary_panel_payload(
            count, status, tone=tone, seed=seed, phase=phase
        )
        summary_payload = (
            "阿尔宙斯 MMO Seed 助手", tuple(summary_lines), tuple(summary_chips), summary_tone, None
        )
        if _PANEL_LAST_PAYLOADS.get("seed-capture") != summary_payload:
            try:
                panel.show(
                    "seed-capture", "阿尔宙斯 MMO Seed 助手", summary_lines,
                    chips=summary_chips, tone=summary_tone,
                )
                _PANEL_LAST_PAYLOADS["seed-capture"] = summary_payload
            except Exception as exc:
                render_errors.append(f"状态卡：{exc}")

        for index in range(1, 5):
            detail = details[index - 1] if index <= count and index <= len(details) else None
            title, lines, chips, row_tone = _pokemon_panel_payload(index, detail)
            try:
                image_path = _panel_icon_path(detail)
            except Exception as exc:
                image_path = None
                render_errors.append(f"第 {index} 张卡片图标：{exc}")
            payload = (title, tuple(lines), tuple(chips), row_tone, str(image_path) if image_path else None)
            card_id = f"seed-mon-{index}"
            if _PANEL_LAST_PAYLOADS.get(card_id) == payload:
                continue
            kwargs = {"chips": chips, "tone": row_tone}
            if image_path:
                kwargs["image"] = image_path
            try:
                panel.show(card_id, title, lines, **kwargs)
                _PANEL_LAST_PAYLOADS[card_id] = payload
            except Exception as exc:
                render_errors.append(f"第 {index} 张卡片：{exc}")

        _PANEL_INITIALIZED = True
        if render_errors:
            short_errors = "；".join(str(error)[:120] for error in render_errors[:4])
            print(f"[Seed 界面] 部分卡片绘制失败：{short_errors}", flush=True)
            for line in _fallback_result_lines(details, count, seed=seed, phase=phase, status=status):
                print(f"[Seed 结果] {line}", flush=True)
        else:
            print(
                f"[Seed 界面] 原生紧凑卡片已更新：收录 {count}/4；固定四卡布局，未变化的卡片不会重绘。",
                flush=True,
            )

    # Persist detail text/JSON when the selected set changes, and once at final status;
    # solver progress callbacks must not rewrite the same files repeatedly.
    record_signature = tuple(str(Path(path)) for path in records)
    final_phases = ("已完成", "计算失败", "缺少计算程序", "采集失败", "无法安全启动")
    should_write_details = bool(records) and (
        record_signature != _DETAILS_FILE_RECORD_SIGNATURE
        or (phase in final_phases and phase != _DETAILS_FILE_FINAL_PHASE)
    )
    if should_write_details:
        details_path = write_pokemon_details_file(records, details=details)
        if details_path and phase in final_phases:
            print(f"[Seed 详情] 其他详细信息已保存：{details_path}", flush=True)
        if records:
            _DETAILS_FILE_RECORD_SIGNATURE = record_signature
        if phase in final_phases:
            _DETAILS_FILE_FINAL_PHASE = phase

    try:
        from pokeldn import progress
        if phase == "收录中":
            progress.show(count, 4, "收录初始宝可梦", unit="items")
        elif phase in ("结束联机中", "计算中"):
            progress.show(count, 4, "阿尔宙斯 MMO Seed", unit="items")
        elif phase == "已完成":
            progress.show(4, 4, "阿尔宙斯 MMO Seed", unit="items")
    except Exception:
        pass


class QuietHostOutput:
    """Hide noisy host packet logs while passing plugin/solver status through to Pokeldn."""
    _ERROR_WORDS = (
        "failed", "failure", "traceback", "exception", "error:",
        "not found", "timed out", "permission denied", "失败", "无法",
    )
    _PLUGIN_PREFIXES = (
        "[seed", "[panel]", "[progress]", "[popup]", "pla seed finder",
        "pid:", "ec:", "ivs:", "instruction set:", "threads:", "searching...",
        "time:", "found a multi-spawn group seed", "no group seeds found",
        "press [enter] to exit", "==========", "【最终群体 seed】",
    )

    def __init__(self, target, on_connected=None, on_leave_sent=None, on_switch_left=None):
        self.target = target
        self.on_connected = on_connected
        self.on_leave_sent = on_leave_sent
        self.on_switch_left = on_switch_left
        self._buffer = ""
        self._lock = threading.RLock()
        self._handle_lock = threading.RLock()
        self.lines = deque(maxlen=120)
        self.errors = deque(maxlen=30)
        self.encoding = getattr(target, "encoding", "utf-8")

    def write(self, value):
        if not isinstance(value, str):
            value = str(value)
        with self._lock:
            self._buffer += value
            ready = []
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                ready.append(line.rstrip("\r"))
        for line in ready:
            self._handle_line(line)
        return len(value)

    def _handle_line(self, line):
        with self._handle_lock:
            stripped = line.strip()
            if not stripped:
                return
            lower = stripped.lower()
            self.lines.append(stripped)
            if any(word in lower for word in self._ERROR_WORDS):
                self.errors.append(stripped)
            if self.on_connected and (
                "console joined" in lower or "a console joined the network" in lower
            ):
                callback, self.on_connected = self.on_connected, None
                callback()
            if self.on_leave_sent and "session leave request (type 3)" in lower:
                self.on_leave_sent()
            if self.on_switch_left and (
                "console left after the trade" in lower or "left the session; the run ends here" in lower
            ):
                self.on_switch_left()

            # Keep routine [pla] packet chatter hidden, but show every plugin status and
            # the solver's progress/results even while the radio host is still closing.
            if lower.startswith(self._PLUGIN_PREFIXES) or re.fullmatch(r"[0-9]{15,20}", stripped):
                self.target.write(line + "\n")
                self.target.flush()

    def flush(self):
        with self._lock:
            tail, self._buffer = self._buffer, ""
        if tail:
            self._handle_line(tail.rstrip("\r"))
        self.target.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self.target.fileno()


def load_host():
    from pokeldn.app.paths import ROOT
    import importlib
    import importlib.util

    path = Path(ROOT) / "bin" / "pla_host.py"
    if path.is_file():
        spec = importlib.util.spec_from_file_location("pokeldn_plugin_pla_seed_host", path)
        if not spec or not spec.loader:
            raise RuntimeError(f"无法加载 Pokeldn 阿尔宙斯交换主程序：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    try:
        return importlib.import_module("bin.pla_host")
    except ImportError as exc:
        raise RuntimeError(
            "当前 Pokeldn 安装中找不到 bin/pla_host.py。请确认使用的是包含《阿尔宙斯》本地交换功能的桌面版。"
        ) from exc


def make_offer(root, trainer):
    """Create a legal, harmless PLA offer record for the exchange preview."""
    from pokeldn import pokemon as pokemon_service

    service = pokemon_service.SERVICE
    species_rows = service.species("pla")
    species = {int(row["id"]): str(row["name"]) for row in species_rows}
    # Prefer ordinary early-game species; fall back to any species available in PLA.
    preferred = (399, 396, 403, 418, 425, 443, 46, 52, 25, 133, 16)
    candidates = [sid for sid in preferred if sid in species]
    candidates.extend(sid for sid in species if sid not in candidates)

    errors = []
    offer_path = root / "offer" / "seed-preview.pa8"
    offer_path.parent.mkdir(parents=True, exist_ok=True)
    for species_id in candidates:
        try:
            made = service.make(
                "pla", species_id, trainer,
                level=0, shiny=False, nickname="", version="", options={},
            )
            if not made.get("legal"):
                errors.append(f"{species.get(species_id, species_id)}：PKHeX 未判定为合法")
                continue
            source = Path(str(made.get("file", "")))
            if not source.is_file():
                errors.append(f"{species.get(species_id, species_id)}：生成记录文件不存在")
                continue
            # Copy bytes into this plugin's own data folder; do not move a temp file.
            offer_path.write_bytes(source.read_bytes())
            print(f"[Seed] 已准备合法的交换预览宝可梦：{species.get(species_id, species_id)}。不会自动确认交换。", flush=True)
            return offer_path
        except Exception as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else "没有找到可用物种。"
    raise RuntimeError(f"无法生成用于打开交换预览的合法《阿尔宙斯》宝可梦：{detail}")


def resolve_keys_path(raw_path):
    if not raw_path or not str(raw_path).strip():
        raise ValueError("请在工具设置中填写你自己的 prod.keys 文件路径。")

    raw = str(raw_path).strip().strip('"').strip("'")
    # Expand Windows %ENVVAR% and POSIX $ENVVAR syntax.
    raw = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        raw,
    )
    raw = os.path.expandvars(raw)

    # Normalize Windows separators for non-Windows test/dev environments.
    # On Windows, keep native backslashes intact.
    if os.name != "nt":
        raw = raw.replace("\\", os.sep)

    expanded = os.path.expanduser(raw)
    if expanded == raw and raw.startswith("~") and len(raw) > 1 and raw[1] in ("\\", "/"):
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        if home:
            expanded = str(Path(home) / raw[2:].lstrip("\\/").replace("\\", os.sep))

    path = Path(expanded)
    if not path.is_file():
        raise FileNotFoundError(
            "Pokeldn 传入的 prod.keys 路径不存在或不可访问："
            f"{path}\n请先在 Pokeldn「设置 → Switch 密钥」中确认密钥路径和文件存在，"
            "然后重启应用并重新运行本工具。"
            "不要从陌生网站下载或使用他人的密钥文件。"
        )
    return path.resolve()


def host_arguments(args, offer_path, capture_dir, keys_path, seconds_override=None):
    if not (isinstance(args.code, str) and re.fullmatch(r"[0-9]{8}", args.code)):
        raise ValueError("本地交换密码必须是 8 位 ASCII 数字（例如 12345678）。")
    if not 1 <= args.channel <= 13:
        raise ValueError("无线信道必须在 1 到 13 之间。")
    if not 120 <= args.seconds <= 3600:
        raise ValueError("最长等待时间必须在 120 到 3600 秒之间。")

    run_seconds = max(1, int(seconds_override if seconds_override is not None else args.seconds))
    return [
        "--keys", str(keys_path),
        "--code", args.code,
        "--channel", str(args.channel),
        "--seconds", str(run_seconds),
        "--session-update",
        "--sustain",
        "--clock",
        "--data-exchange",
        "--data-exchange-name", "POKELDN",
        "--data-exchange-id", "11223344",
        "--game-channel",
        "--trade-box",
        "--trade-box-record", str(offer_path),
    ]


def save_four_records(records, target):
    """Save four records in selection order with descriptive names for the UI parser.

    EtumrepMMO receives numbered copies in its own solver/mons folder. Keeping
    descriptive names here lets the UI still recover species and level even when
    PKHeX's optional summary API has a hiccup.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.pa8"):
        try:
            old.unlink()
        except OSError:
            pass
    chosen = list(records or [])[:4]
    for index, source in enumerate(chosen, start=1):
        source = Path(source)
        source_name = source.name
        if re.match(r"\d{4}_.+?_lv\d+", source.stem, re.I):
            # Preserve the original species/level prefix and the capture digest.
            filename = f"{index:02d}_{source_name}"
        else:
            detail = record_details(source)
            sid = detail.get("species_id")
            species = str(detail.get("species_en") or detail.get("species") or "").strip()
            level = str(detail.get("level") or "?")
            clean_species = re.sub(r"[^A-Za-z0-9\u3400-\u9fff.\-]+", "_", species).strip("_")
            if sid is not None and clean_species and level.isdigit():
                filename = f"{index:02d}_{int(sid):04d}_{clean_species}_lv{level}.pa8"
            else:
                filename = f"{index:02d}.pa8"
        (target / filename).write_bytes(source.read_bytes())
    return target


def find_latest_mons(root):
    runs = root / "runs"
    candidates = []
    if runs.exists():
        for mons in runs.glob("*/mons"):
            files = list(mons.glob("*.pa8"))
            if len(files) >= 4:
                candidates.append((mons.stat().st_mtime_ns, mons))
    if not candidates:
        raise RuntimeError(
            "找不到最近一次完整的四只宝可梦记录。请先运行「MMO 测 Seed（收集四只）」。"
        )
    return max(candidates, key=lambda item: item[0])[1]


def find_solver(root):
    solver_dir = root / "solver"
    solver_dir.mkdir(parents=True, exist_ok=True)
    for name in SOLVER_EXE_NAMES:
        candidate = solver_dir / name
        if candidate.is_file():
            return candidate
    for name in SOLVER_EXE_NAMES:
        matches = list(solver_dir.rglob(name))
        if matches:
            return matches[0]
    # Support versioned official release names such as
    # EtumrepMMO.ConsoleApp.1.2.0.exe, in the solver root or nested folder.
    for pattern in SOLVER_EXE_GLOBS:
        matches = sorted(solver_dir.rglob(pattern))
        if matches:
            return matches[0]
    return None


def run_solver(root, mons_dir):
    exe = find_solver(root)
    if exe is None:
        solver_dir = root / "solver"
        record_paths = sorted(Path(mons_dir).glob("*.pa8"))[:4]
        missing_status = f"缺少 EtumrepMMO 计算程序。请将官方 Windows x64 发布包全部文件解压到：{solver_dir}"
        print("[Seed] 四只宝可梦记录已保存，但没有找到 EtumrepMMO 计算程序。", flush=True)
        print("[Seed] 官方下载页：https://github.com/kwsch/EtumrepMMO/releases/latest", flush=True)
        print(f"[Seed] 请把官方 Windows x64 发布包中的全部文件解压到：{solver_dir}", flush=True)
        print("[Seed] 需要 .NET Runtime 8.0 和 Microsoft Visual C++ Redistributable。安装后运行「MMO 测 Seed（计算最近记录）」即可，无需重新采集。", flush=True)
        update_collection_ui(4, missing_status, "warning", records=record_paths, phase="缺少计算程序")
        return 2

    solver_dir = exe.parent
    solver_mons = solver_dir / "mons"
    solver_mons.mkdir(parents=True, exist_ok=True)
    for old in solver_mons.glob("*.pa8"):
        try:
            old.unlink()
        except OSError:
            pass
    files = sorted(Path(mons_dir).glob("*.pa8"))[:4]
    if len(files) != 4:
        raise RuntimeError(f"反推需要四只记录，目前只有 {len(files)} 只。")
    for index, source in enumerate(files, start=1):
        (solver_mons / f"{index:02d}.pa8").write_bytes(source.read_bytes())

    print("[Seed] 已准备四只初始捕获记录，启动 EtumrepMMO 反推。", flush=True)
    print(f"[Seed] 输入目录：{solver_mons}", flush=True)
    seed_result = None
    group_seed_failure = False
    current_entity = 1
    last_ui_update = 0.0
    record_paths = sorted(Path(mons_dir).glob("*.pa8"))[:4]
    update_collection_ui(4, "四只记录已就绪，开始反推群体 Seed。", "info",
                         records=record_paths, phase="计算中")
    try:
        proc = subprocess.Popen(
            [str(exe)],
            cwd=str(solver_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                print(text, flush=True)
                entity_match = re.search(r"Checking entity\s+(\d+)/4", text, re.I)
                if entity_match:
                    current_entity = int(entity_match.group(1))
                    try:
                        from pokeldn import progress
                        progress.show(current_entity - 1, 4, "反推 MMO Seed", unit="items")
                    except Exception:
                        pass
                    update_collection_ui(4, f"正在检查第 {current_entity}/4 只宝可梦记录。", "info",
                                         records=record_paths, phase="计算中")
                percent_match = re.search(r"Searching\.\.\.\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
                if percent_match and time.monotonic() - last_ui_update >= 0.8:
                    last_ui_update = time.monotonic()
                    percent = max(0.0, min(100.0, float(percent_match.group(1))))
                    try:
                        from pokeldn import progress
                        progress.show((current_entity - 1) + percent / 100.0, 4,
                                      f"反推 Seed · 第 {current_entity}/4 只", unit="items")
                    except Exception:
                        pass
                if "no group seeds found" in text.lower():
                    group_seed_failure = True
                    print("[Seed 计算失败] EtumrepMMO 明确返回：没有找到群体 Seed。", flush=True)
                decimal_seed = re.fullmatch(r"\s*(\d{15,20})\s*", text)
                if decimal_seed and not group_seed_failure:
                    seed_result = decimal_seed.group(1)
                    update_collection_ui(4, "计算成功：已找到最终群体 Seed。", "ok",
                                         records=record_paths, seed=seed_result, phase="已完成")
                if "seed" in text.lower() or "匹配" in text or "match" in text.lower():
                    print(f"[Seed 结果提示] {text}", flush=True)
        code = proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("EtumrepMMO 运行超过 60 分钟，已停止。")
    except FileNotFoundError as exc:
        raise RuntimeError(f"无法启动计算程序：{exe}。请确认解压了官方发布包的全部文件。") from exc

    if code:
        raise RuntimeError(
            f"EtumrepMMO 退出代码为 {code}。请检查是否安装 .NET Runtime 8.0 和 Microsoft Visual C++ Redistributable。"
        )
    if group_seed_failure:
        failure_status = (
            "计算失败：EtumrepMMO 没有找到群体 Seed。"
            "四只记录已保存，但不能把单只宝可梦的十六进制候选值当作最终 Seed。"
            "请核对四只是否来自同一次 MMO 群体，并且都是最初捕获记录。"
        )
        print("[Seed 计算失败] 没有找到群体 Seed。四只记录已保存，不需要立刻重新采集。", flush=True)
        print("[Seed 计算失败] 请先核对四只记录是否来自同一次 MMO 群体、是否均为最初捕获宝可梦；单只候选值不是最终群体 Seed。", flush=True)
        update_collection_ui(4, failure_status, "warning",
                             records=record_paths, phase="计算失败")
        print("[Seed] 计算状态：失败（没有找到群体 Seed）。", flush=True)
        print("[Seed] EtumrepMMO 已结束。", flush=True)
        return 1
    elif seed_result:
        print("[Seed 计算成功] 已找到最终群体 Seed。", flush=True)
        print("========== 最终群体 Seed（右侧输出可复制） ==========", flush=True)
        print(f"【最终群体 Seed】{seed_result}", flush=True)
        print(seed_result, flush=True)
        print("====================================================", flush=True)
        try:
            (root / "latest_seed.txt").write_text(seed_result + "\n", encoding="utf-8")
            print(f"[Seed 备份] 已保存到：{root / 'latest_seed.txt'}", flush=True)
        except OSError:
            pass
        update_collection_ui(4, "计算成功。Seed 已在下方大字显示，文本备份已保存。", "ok",
                             records=record_paths, seed=seed_result, phase="已完成")
        open_seed_text_file(root / "latest_seed.txt")
    else:
        failure_status = (
            "计算未得到可确认的最终 Seed。程序虽正常退出，但输出中没有最终群体 Seed。"
            "请查看日志末尾，勿将单只宝可梦候选值当作最终结果。"
        )
        print("[Seed 计算失败] 没有提取到最终群体 Seed；程序退出码为 0 不代表计算成功。", flush=True)
        update_collection_ui(4, failure_status, "warning",
                             records=record_paths, phase="计算失败")
        print("[Seed] 计算状态：失败（没有可确认的最终群体 Seed）。", flush=True)
        print("[Seed] EtumrepMMO 已结束。", flush=True)
        return 1
    print("[Seed] 计算状态：成功。", flush=True)
    print("[Seed] EtumrepMMO 已结束。", flush=True)
    return 0


def install_graceful_leave_hooks(host, stop_event, switch_left_event=None, grace_seconds=18.0):
    """Send a normal leave as soon as four confirmed offers are saved.

    Keep the radio session alive until Pokeldn observes the Switch leaving; the grace
    timeout is only a safety fallback for host builds that do not expose that event.
    """
    original_build_parser = getattr(host, "build_parser", None)
    if not callable(original_build_parser):
        return None
    try:
        probe = original_build_parser()
        options = {
            option
            for action in getattr(probe, "_actions", [])
            for option in action.option_strings
        }
        if "--leave-after" not in options or "--stay-after-leave" not in options:
            return None
    except Exception:
        return None

    state = {"grace_started": None}
    original_left_after_trade = getattr(host, "left_after_trade", None)
    if callable(original_left_after_trade) and switch_left_event is not None:
        def wrapped_left_after_trade(*args, **kwargs):
            result = original_left_after_trade(*args, **kwargs)
            if result and stop_event.is_set():
                switch_left_event.set()
            return result
        host.left_after_trade = wrapped_left_after_trade

    class DynamicHostArgs:
        def __init__(self, base):
            self._base = base

        def __getattr__(self, name):
            if name == "leave_after":
                return 0.0 if stop_event.is_set() else getattr(self._base, name)
            if name == "stay_after_leave":
                if not stop_event.is_set():
                    return getattr(self._base, name)
                # The host emits this event only after it notices the Switch has left.
                # Return False on the next loop so the host exits immediately then.
                if switch_left_event is not None and switch_left_event.is_set():
                    return False
                if state["grace_started"] is None:
                    state["grace_started"] = time.monotonic()
                return time.monotonic() - state["grace_started"] < grace_seconds
            return getattr(self._base, name)

    def wrapped_build_parser():
        parser = original_build_parser()
        original_parse_args = parser.parse_args

        def wrapped_parse_args(*args, **kwargs):
            return DynamicHostArgs(original_parse_args(*args, **kwargs))

        parser.parse_args = wrapped_parse_args
        return parser

    host.build_parser = wrapped_build_parser

    def restore():
        host.build_parser = original_build_parser
        if callable(original_left_after_trade):
            host.left_after_trade = original_left_after_trade

    return restore


def collect_and_solve(args):
    root = plugin_data()
    # Prune old history BEFORE creating the new run directory. The active run is also
    # protected during later pruning calls, so an empty fresh session cannot be removed.
    retention = prune_run_history(root, keep_pokemon=12, keep_incomplete_runs=2)
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{time.time_ns() % 1_000_000_000:09d}"
    run_dir = root / "runs" / run_id
    capture_dir = run_dir / "confirmed-offers"
    mons_dir = run_dir / "mons"
    capture_dir.mkdir(parents=True, exist_ok=True)
    reset_collection_panel_state()

    # Bound disk growth: preserve the latest 12 selected Pokemon (three complete runs)
    # and at most two incomplete sessions for troubleshooting.
    if retention["complete_deleted"] or retention["incomplete_deleted"]:
        print(
            "[Seed 历史] 已清理旧记录："
            f"删除完整采集 {retention['complete_deleted']} 次，"
            f"删除未完成采集 {retention['incomplete_deleted']} 次；"
            "保留最近 12 只宝可梦记录。",
            flush=True,
        )

    keys_path = resolve_keys_path(args.keys)
    trainer = trainer_from_arg(args.trainer)
    offer_path = make_offer(root, trainer)
    host = load_host()
    host_arguments(args, offer_path, capture_dir, keys_path)

    offer_queue = queue.Queue()
    selection_restore = install_confirmed_offer_hook(host, offer_queue)
    if selection_restore is None:
        failure_status = (
            "当前 Pokeldn 联机主程序无法提供可识别的宝可梦交换事件。"
            "为避免光标预览被误收录，本次未启动采集；请更新 Pokeldn 后重试。"
        )
        print(f"[Seed 错误] {failure_status}", flush=True)
        update_collection_ui(0, failure_status, "error", phase="无法安全启动")
        return 1

    stop_monitor = threading.Event()
    four_found = threading.Event()
    leave_sent = threading.Event()
    switch_left = threading.Event()
    graceful_restore = install_graceful_leave_hooks(host, four_found, switch_left_event=switch_left)
    if graceful_restore is None:
        selection_restore()
        failure_status = (
            "当前 Pokeldn 联机主程序未提供正常结束交换所需的接口。"
            "为避免收录完成后直接断开造成 Switch 报错，本次未启动联机采集；"
            "请更新 Pokeldn 后重试。"
        )
        print(f"[Seed 错误] {failure_status}", flush=True)
        update_collection_ui(0, failure_status, "error", phase="无法安全启动")
        return 1

    selected_paths = []
    seen_digests = set()
    selection_lock = threading.RLock()
    solver_state = {"started": False, "thread": None, "code": None, "error": None}
    solver_lock = threading.RLock()

    previous_stdout, previous_stderr = sys.stdout, sys.stderr

    def on_connected():
        print("[Seed] Switch 已连接，请选择第一只最初捕获的宝可梦。", flush=True)

    def on_switch_left():
        # Returning with B between Pokemon is normal; only treat the host's leave
        # observation as final after the fourth record has triggered a leave request.
        if four_found.is_set() and leave_sent.is_set():
            switch_left.set()

    output_filter = QuietHostOutput(
        previous_stdout,
        on_connected=on_connected,
        on_leave_sent=leave_sent.set,
        on_switch_left=on_switch_left,
    )

    def start_solver_worker():
        with solver_lock:
            if solver_state["started"]:
                return
            solver_state["started"] = True

            def worker():
                try:
                    solver_state["code"] = run_solver(root, mons_dir)
                except Exception as exc:
                    solver_state["error"] = exc
                    print(f"[Seed 计算失败] 计算程序运行失败：{exc}", flush=True)
                    update_collection_ui(
                        4, f"计算程序运行失败：{exc}", "error",
                        records=sorted(mons_dir.glob("*.pa8"))[:4], phase="计算失败",
                    )
                    solver_state["code"] = 1

            thread = threading.Thread(target=worker, name="pla-seed-solver", daemon=False)
            solver_state["thread"] = thread
            thread.start()

    def save_and_finish_collection():
        """Persist all four selector-4 records before asking the host to leave."""
        with selection_lock:
            records = list(selected_paths[:4])
        if len(records) != 4:
            return False
        try:
            save_four_records(records, mons_dir)
            (run_dir / ".complete").write_text("complete\n", encoding="utf-8")
            saved_records = sorted(mons_dir.glob("*.pa8"))[:4]
            if len(saved_records) != 4:
                raise RuntimeError(f"四只记录已进入保存流程，但 solver 目录中只有 {len(saved_records)} 只。")
            # The confirmed-offers files are temporary staging copies. Keep the canonical
            # four records only in mons/ so the rolling 12-Pokemon history is not doubled.
            with selection_lock:
                selected_paths[:] = saved_records
            for temporary_record in capture_dir.glob("*.pa8"):
                try:
                    temporary_record.unlink()
                except OSError:
                    pass
            try:
                capture_dir.rmdir()
            except OSError:
                pass
            retention_now = prune_run_history(root, keep_pokemon=12, keep_incomplete_runs=2, protected_run=run_dir)
            print(f"[Seed] 已收录 4/4 只宝可梦，记录已保存：{mons_dir}", flush=True)
            print(
                "[Seed 历史] 当前保留最近 12 只宝可梦信息（最多 3 次完整采集）；"
                f"本次清理旧完整记录 {retention_now['complete_deleted']} 次、"
                f"旧未完成采集 {retention_now['incomplete_deleted']} 次。",
                flush=True,
            )
            update_collection_ui(
                4, "四只已收齐，正在正常结束交换；随后立即反推 Seed。", "ok",
                records=saved_records, phase="结束联机中",
            )
        except Exception as exc:
            solver_state["error"] = exc
            solver_state["code"] = 1
            print(f"[Seed 保存失败] 无法保存四只宝可梦记录：{exc}", flush=True)
            update_collection_ui(4, f"记录保存失败：{exc}", "error", records=records, phase="采集失败")
        # The dynamic host args now make --leave-after become zero. The host sends
        # the protocol's normal leave request; no KeyboardInterrupt/forced radio stop.
        four_found.set()
        if solver_state["error"] is None:
            if leave_sent.wait(2.0):
                print("[Seed] 已发送正常结束交换请求，立即开始反推 Seed。", flush=True)
            else:
                print("[Seed] 已触发正常结束交换流程；未及时观察到离开日志，继续计算已保存的记录。", flush=True)
            start_solver_worker()
        return True

    def monitor_records():
        """Consume only selector-4 events; selector-2 cursor previews never enter this queue."""
        while True:
            while True:
                try:
                    record, digest, _observed_at, hint = offer_queue.get_nowait()
                except queue.Empty:
                    break
                if digest in seen_digests:
                    continue
                with selection_lock:
                    if len(selected_paths) >= 4:
                        continue
                    index = len(selected_paths) + 1
                    try:
                        species_id = int((hint or {}).get("species_id"))
                        level = int((hint or {}).get("level"))
                    except (TypeError, ValueError):
                        species_id, level = 0, 0
                    if species_id > 0 and 1 <= level <= 100:
                        species_label = chinese_species_name("", species_id)
                        clean_species = re.sub(r"[^A-Za-z0-9\u3400-\u9fff.\-]+", "_", species_label).strip("_") or "未知宝可梦"
                        filename = f"{species_id:04d}_{clean_species}_lv{level}_{digest[:8]}.pa8"
                    else:
                        filename = f"{index:02d}_{digest[:12]}.pa8"
                    target = capture_dir / filename
                    try:
                        # Defensive mkdir protects against external cleanup or stale paths;
                        # active-run protection prevents our own retention pass deleting it.
                        atomic_save_pa8(target, record)
                    except OSError as exc:
                        current_count = len(selected_paths)
                        print(f"[Seed 采集警告] 无法保存第 {index} 只确认页记录：{exc}", flush=True)
                        update_collection_ui(
                            current_count,
                            f"第 {index} 只确认页记录保存失败：{exc}。当前未计入收录，请检查输出日志。",
                            "error", records=list(selected_paths), phase="保存失败",
                        )
                        continue
                    selected_paths.append(target)
                    seen_digests.add(digest)
                    count = len(selected_paths)
                    records_now = list(selected_paths)

                retention_now = prune_run_history(root, keep_pokemon=12, keep_incomplete_runs=2, protected_run=run_dir)
                if retention_now["records_deleted"]:
                    print(
                        f"[Seed 历史] 新增确认页记录后清理了 {retention_now['records_deleted']} 只旧记录；"
                        f"当前最多保留最近 12 只。",
                        flush=True,
                    )
                print(
                    f"[Seed] 已识别交换确认页面中的第 {count}/4 只宝可梦；"
                    "光标预览不会触发收录，也不会确认真正交换。",
                    flush=True,
                )
                update_collection_ui(
                    count,
                    f"已从交换确认页面收录第 {count}/4 只；按 B 返回箱子选择下一只，不要确认交换。",
                    "ok" if count == 4 else "info",
                    records=records_now,
                    phase="收录中" if count < 4 else "结束联机中",
                )
                if count >= 4:
                    save_and_finish_collection()
                    return

            if stop_monitor.is_set() and offer_queue.empty():
                return
            stop_monitor.wait(0.1)

    print("[Seed] 已启动阿尔宙斯 MMO Seed 采集。", flush=True)
    print("[Seed] Switch 端：进入祝庆村交换 NPC，选择本地交换并输入相同的 8 位密码。", flush=True)
    print("[Seed] 只有进入‘确定要进行宝可梦的交换吗？’确认页面，才会收录当前选定的宝可梦。", flush=True)
    print("[Seed] 光标移动、停留和普通预览均不会收录；进入确认页面后按 B 返回箱子，继续选择下一只，不要确认交换。", flush=True)
    update_collection_ui(0, "页面已就绪，等待 Switch 连接；仅识别交换确认页面的已选宝可梦。", "info", phase="收录中")
    monitor = threading.Thread(target=monitor_records, name="pla-seed-record-monitor", daemon=True)
    monitor.start()

    host_exception = None
    stop_requested = False
    host_result = None
    attempt = 0
    overall_deadline = time.monotonic() + args.seconds
    try:
        sys.stdout = output_filter
        sys.stderr = output_filter
        previous_argv = sys.argv[:]
        host_entry = str(getattr(host, "__file__", "bin/pla_host.py"))
        while not four_found.is_set():
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt += 1
            output_filter.lines.clear()
            output_filter.errors.clear()
            try:
                host_args = host_arguments(
                    args, offer_path, capture_dir, keys_path,
                    seconds_override=max(1, int(remaining)),
                )
                sys.argv = [host_entry, *host_args]
                host_result = host.main()
            except KeyboardInterrupt:
                if not four_found.is_set():
                    stop_requested = True
                break
            except Exception as exc:
                host_exception = exc
                break
            finally:
                sys.argv = previous_argv

            if four_found.is_set():
                break
            attempt_lines = list(output_filter.lines)
            if any("interrupted" in line.lower() for line in attempt_lines[-12:]):
                stop_requested = True
                break
            if host_result not in (None, 0):
                host_exception = RuntimeError(
                    f"联机主程序退出，代码 {host_result}。请检查右侧输出中的无线连接错误。"
                )
                break
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                break
            if attempt >= 8:
                host_exception = RuntimeError(
                    "联机主程序在未收齐四只时连续提前结束 8 次，已停止自动重连。"
                    "请检查 Switch 是否仍在本地交换界面，以及无线连接是否稳定。"
                )
                break
            with selection_lock:
                count_now = len(selected_paths)
                records_now = list(selected_paths)
            print(
                f"[Seed] 联机主程序提前结束；当前已收录 {count_now}/4。"
                f"正在自动重启连接（第 {attempt} 次）。",
                flush=True,
            )
            update_collection_ui(
                count_now,
                "联机连接提前结束，正在自动重启。若 Switch 已返回 NPC，请用相同密码重新进入本地交换。",
                "warning", records=records_now, phase="收录中",
            )
            time.sleep(min(2.0, max(0.0, remaining)))
    finally:
        output_filter.flush()
        sys.stdout, sys.stderr = previous_stdout, previous_stderr
        stop_monitor.set()
        monitor.join(timeout=3.0)
        try:
            selection_restore()
        except Exception as exc:
            print(f"[Seed] 恢复宝可梦事件识别钩子时出现警告：{exc}", flush=True)
        try:
            graceful_restore()
        except Exception as exc:
            print(f"[Seed] 恢复联机主程序钩子时出现警告：{exc}", flush=True)

    with selection_lock:
        records = list(selected_paths[:4])
        count = len(records)

    if count >= 4:
        solver_thread = solver_state.get("thread")
        if solver_thread is not None:
            solver_thread.join()
            if solver_state.get("error") is not None:
                print(f"[Seed 计算失败] {solver_state['error']}", flush=True)
            return int(solver_state.get("code") if solver_state.get("code") is not None else 1)
        if solver_state.get("error") is not None:
            return int(solver_state.get("code") or 1)
        # Defensive fallback: if the event handler saved all four but the worker did not
        # start (e.g. an unexpected thread startup error), calculate once here.
        try:
            save_four_records(records, mons_dir)
            return run_solver(root, mons_dir)
        except Exception as exc:
            print(f"[Seed 计算失败] 计算程序运行失败：{exc}", flush=True)
            update_collection_ui(4, f"计算程序运行失败：{exc}", "error", records=records, phase="计算失败")
            return 1

    if stop_requested:
        final_status = f"已手动停止采集，当前已收录 {count}/4；已收录信息保留在下方卡片。"
        final_tone = "warning"
        print(f"[Seed] 已手动停止采集：当前已收录 {count}/4 只宝可梦。", flush=True)
    elif host_exception is not None:
        final_status = f"联机程序未能完成，当前已收录 {count}/4。请检查右侧输出中的错误信息。"
        final_tone = "error"
        print(f"[Seed] 联机主程序失败：{host_exception}", flush=True)
    elif time.monotonic() >= overall_deadline:
        final_status = f"等待时间已结束，当前已收录 {count}/4。可增加最长等待时间后重试。"
        final_tone = "warning"
        print(f"[Seed] 等待时间结束：当前已收录 {count}/4 只宝可梦。", flush=True)
    else:
        final_status = f"联机采集意外结束，当前已收录 {count}/4。请检查连接后重新进入本地交换。"
        final_tone = "error"
        print(f"[Seed] 联机采集意外结束：已收录 {count}/4 只宝可梦。", flush=True)

    update_collection_ui(count, final_status, final_tone, records=records, phase="采集失败")
    print(f"[Seed] 当前确认页记录保存在：{capture_dir}", flush=True)
    return 1


def solve_last(root=None):
    reset_collection_panel_state()
    root = root or plugin_data()
    mons_dir = find_latest_mons(root)
    print(f"[Seed] 使用最近一次记录：{mons_dir}", flush=True)
    return run_solver(root, mons_dir)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "solve-last":
            return solve_last()
        return collect_and_solve(args)
    except KeyboardInterrupt:
        print("[Seed] 已收到停止请求。", flush=True)
        return 130
    except Exception as exc:
        print(f"[Seed] 失败：{exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
