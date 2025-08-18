# accessibility_user_settings.py

import os
import re
import json
import winreg
import psutil
import platform
import configparser
from pathlib import Path

# ---------------------- tiny utils ----------------------

def _to_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1","true","yes","on"}: return True
    if s in {"0","false","no","off"}: return False
    return None

def _to_int(v):
    try: return int(str(v).strip())
    except Exception: return None

def _ci_get(d: dict, key: str, default=None):
    if not isinstance(d, dict): return default
    lk = key.lower()
    for k, v in d.items():
        if str(k).lower() == lk:
            return v
    return default

def _ci_section(cfg: dict, sec_name: str):
    for s, body in (cfg or {}).items():
        if str(s).lower() == sec_name.lower():
            return body or {}
    return {}

# ---------------------- robust INI reader ----------------------

def _parse_with_configparser(text: str):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # preserve case
    cp.read_string(text)
    return {sec: dict(cp.items(sec)) for sec in cp.sections()}

def _manual_ini_parse(text: str):
    """
    Super-tolerant INI fallback: supports ;/# comments, [sections], and key=value.
    """
    cfg = {}
    sec = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#",";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1].strip()
            if sec not in cfg:
                cfg[sec] = {}
            continue
        if "=" in line and sec:
            k, v = line.split("=", 1)
            cfg[sec][k.strip()] = v.strip()
    return cfg

def _read_ini_any_encoding(path: Path):
    """
    Try a few likely encodings; use ConfigParser first, then manual fallback.
    Returns {} if file missing or unreadable.
    """
    if not path or not path.exists():
        return {}
    encs = ("utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "cp1252")
    # First pass: ConfigParser for each encoding
    for enc in encs:
        try:
            text = path.read_text(encoding=enc, errors="strict")
            return _parse_with_configparser(text)
        except Exception:
            continue
    # Second pass: manual parser with lenient decoding
    for enc in encs:
        try:
            text = path.read_text(encoding=enc, errors="replace")
            parsed = _manual_ini_parse(text)
            if parsed:
                return parsed
        except Exception:
            continue
    return {}

# ---------------------- NVDA helpers ----------------------

# --- add this helper near the other NVDA helpers ---

def _nvda_fill_from_synth_sections(cfg: dict, out_speech: dict):
    """
    If voice/rate/volume/pitch are still None, scan [synthSettings.*] sections.
    Take the first section that provides a value.
    """
    wanted_map = {
        "voice": ["voice", "voiceName", "Voice"],
        "rate": ["rate", "Rate", "speed", "Speed"],
        "pitch": ["pitch", "Pitch"],
        "volume": ["volume", "Volume"],
    }
    for sec, body in (cfg or {}).items():
        if not str(sec).lower().startswith("synthsettings."):
            continue
        for field, keys in wanted_map.items():
            if out_speech.get(field) is None:
                for k in keys:
                    v = _ci_get(body, k)
                    if v is not None:
                        out_speech[field] = _to_int(v) if field in ("rate","pitch","volume") else v
                        break

def collect_nvda_settings_curated():
    """
    Compact, user-meaningful NVDA settings from nvda.ini (+ profile .ini overrides).
    Falls back to [synthSettings.*] for speech params if [speech] is sparse.
    """
    root = _nvda_user_dir()
    running = any((p.info["name"] or "").lower() == "nvda.exe" for p in psutil.process_iter(["name"]))
    out = {
        "present": bool(root),
        "running": running,
        "sources": {},
        "speech": {},
        "braille": {},
        "audio": {},
        "vision": {},
        "keyboard": {},
        "mouse": {},
        "documentFormatting": {},
        "objectPresentation": {},
        "profiles_overrides": {},
        "_debug": {}  # <--- small, safe debug
    }
    if not root:
        return out

    nvda_ini = Path(root) / "nvda.ini"
    cfg = _read_ini_any_encoding(nvda_ini) if nvda_ini.exists() else {}
    out["sources"]["nvda.ini"] = str(nvda_ini) if nvda_ini.exists() else None
    out["_debug"]["nvda_ini_exists"] = nvda_ini.exists()
    out["_debug"]["sections"] = sorted(list(cfg.keys()))

    # Sections (case-insensitive)
    s_speech  = _ci_section(cfg, "speech")
    s_braille = _ci_section(cfg, "braille")
    s_audio   = _ci_section(cfg, "audio")
    s_vision  = _ci_section(cfg, "vision")
    s_kbd     = _ci_section(cfg, "keyboard")
    s_mouse   = _ci_section(cfg, "mouse")
    s_doc     = _ci_section(cfg, "documentFormatting")
    s_obj     = _ci_section(cfg, "objectPresentation")

    # speech
    out["speech"] = {
        "synth": _ci_get(s_speech, "synth") or _ci_get(s_speech, "synthesizer"),
        "voice": _ci_get(s_speech, "voice"),
        "rate": _to_int(_ci_get(s_speech, "rate")),
        "pitch": _to_int(_ci_get(s_speech, "pitch")),
        "volume": _to_int(_ci_get(s_speech, "volume")),
        "inflection": _to_int(_ci_get(s_speech, "inflection")),
        "punctuationLevel": (_ci_get(s_speech, "punctuation")
                             or _ci_get(s_speech, "punctuationLevel")),
        "sayAllStyle": _ci_get(s_speech, "sayAllStyle"),
    }

    # FALLBACK: pull missing voice/rate/volume/pitch from synthSettings.*
    _nvda_fill_from_synth_sections(cfg, out["speech"])

    # synth-specific settings (raw)
    synth_opts = {}
    for sec, body in (cfg or {}).items():
        if str(sec).lower().startswith("synthsettings."):
            synth_opts[sec] = body or {}
    if synth_opts:
        out["speech"]["synthSettings"] = synth_opts

    # braille
    out["braille"] = {
        "display": _ci_get(s_braille, "display"),
        "port": _ci_get(s_braille, "port"),
        "inputTable": _ci_get(s_braille, "inputTable"),
        "outputTable": _ci_get(s_braille, "outputTable"),
        "tetherTo": _ci_get(s_braille, "tetherTo"),
        "showCursor": _to_bool(_ci_get(s_braille, "showCursor")),
        "cursorBlinkRate": _to_int(_ci_get(s_braille, "cursorBlinkRate")),
        "expandToComputerBraille": _to_bool(_ci_get(s_braille, "expandToComputerBraille")),
    }

    # audio
    out["audio"] = {
        "audioDuckingMode": _ci_get(s_audio, "audioDuckingMode"),
        "beepSpeechMode": _ci_get(s_audio, "beepSpeechMode"),
        "outputDevice": _ci_get(s_audio, "outputDevice"),
    }

    # vision
    out["vision"] = {
        "enabledProviders": _ci_get(s_vision, "enabledProviders"),
        "screenCurtain": _to_bool(_ci_get(s_vision, "screenCurtain")),
        "focusHighlight": _to_bool(_ci_get(s_vision, "focusHighlight")),
        "caretHighlight": _to_bool(_ci_get(s_vision, "caretHighlight")),
        "hoverHighlight": _to_bool(_ci_get(s_vision, "hoverHighlight")),
    }

    # keyboard
    out["keyboard"] = {
        "keyboardLayout": _ci_get(s_kbd, "keyboardLayout"),
        "useCapsLockAsNVDAModifierKey": _to_bool(_ci_get(s_kbd, "useCapsLockAsNVDAModifierKey")),
        "useNumpadInsertAsNVDAModifierKey": _to_bool(_ci_get(s_kbd, "useNumpadInsertAsNVDAModifierKey")),
        "useExtendedInsertAsNVDAModifierKey": _to_bool(_ci_get(s_kbd, "useExtendedInsertAsNVDAModifierKey")),
        "speakTypedCharacters": _to_bool(_ci_get(s_kbd, "speakTypedCharacters")),
        "speakTypedWords": _to_bool(_ci_get(s_kbd, "speakTypedWords")),
        "speakCommandKeys": _to_bool(_ci_get(s_kbd, "speakCommandKeys")),
    }

    # mouse
    out["mouse"] = {
        "enableMouseTracking": _to_bool(_ci_get(s_mouse, "enableMouseTracking")),
        "reportObjectUnderMouse": _to_bool(_ci_get(s_mouse, "reportObjectUnderMouse")),
        "audioCoordinatesEnable": _to_bool(_ci_get(s_mouse, "audioCoordinatesEnable")),
    }

    # doc/object formatting — include whatever is present
    out["documentFormatting"] = {
        k: (_to_bool(v) if _to_bool(v) is not None else v) for k, v in (s_doc or {}).items()
    }
    out["objectPresentation"] = {
        k: (_to_bool(v) if _to_bool(v) is not None else v) for k, v in (s_obj or {}).items()
    }

    # profile overrides (raw)
    prof_dir = Path(root) / "profiles"
    if prof_dir.is_dir():
        for prof_ini in prof_dir.glob("*.ini"):
            pcfg = _read_ini_any_encoding(prof_ini)
            body = {}
            for sec in ("speech","braille","audio","vision","keyboard","mouse","documentFormatting","objectPresentation"):
                s = _ci_section(pcfg, sec)
                if s:
                    body[sec] = s
            if body:
                out["profiles_overrides"][prof_ini.stem] = body

    return out


def _nvda_user_dir():
    # Installed path
    appdata = os.environ.get("APPDATA") or ""
    installed = Path(appdata) / "nvda"
    if installed.is_dir():
        return installed
    # Portable path (if NVDA.exe running)
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info.get("name") or "").lower() == "nvda.exe":
            exe = p.info.get("exe")
            if exe:
                cand = Path(exe).parent / "userConfig"
                if cand.is_dir():
                    return cand
    return None

def collect_nvda_settings_curated():
    """
    Compact, user-meaningful NVDA settings from nvda.ini (+ profile .ini overrides).
    """
    root = _nvda_user_dir()
    running = any((p.info["name"] or "").lower() == "nvda.exe" for p in psutil.process_iter(["name"]))
    out = {
        "present": bool(root),
        "running": running,
        "sources": {},
        "speech": {},
        "braille": {},
        "audio": {},
        "vision": {},
        "keyboard": {},
        "mouse": {},
        "documentFormatting": {},
        "objectPresentation": {},
        "profiles_overrides": {}
    }
    if not root:
        return out

    nvda_ini = root / "nvda.ini"
    cfg = _read_ini_any_encoding(nvda_ini) if nvda_ini.exists() else {}
    out["sources"]["nvda.ini"] = str(nvda_ini) if nvda_ini.exists() else None

    # Sections (case-insensitive)
    s_speech  = _ci_section(cfg, "speech")
    s_braille = _ci_section(cfg, "braille")
    s_audio   = _ci_section(cfg, "audio")
    s_vision  = _ci_section(cfg, "vision")
    s_kbd     = _ci_section(cfg, "keyboard")
    s_mouse   = _ci_section(cfg, "mouse")
    s_doc     = _ci_section(cfg, "documentFormatting")
    s_obj     = _ci_section(cfg, "objectPresentation")

    # speech
    out["speech"] = {
        "synth": _ci_get(s_speech, "synth") or _ci_get(s_speech, "synthesizer"),
        "voice": _ci_get(s_speech, "voice"),
        "rate": _to_int(_ci_get(s_speech, "rate")),
        "pitch": _to_int(_ci_get(s_speech, "pitch")),
        "volume": _to_int(_ci_get(s_speech, "volume")),
        "inflection": _to_int(_ci_get(s_speech, "inflection")),
        "punctuationLevel": (_ci_get(s_speech, "punctuation")
                             or _ci_get(s_speech, "punctuationLevel")),
        "sayAllStyle": _ci_get(s_speech, "sayAllStyle"),
    }
    # synth-specific settings
    synth_opts = {}
    for sec, body in (cfg or {}).items():
        if str(sec).lower().startswith("synthsettings."):
            synth_opts[sec] = body or {}
    if synth_opts:
        out["speech"]["synthSettings"] = synth_opts

    # braille
    out["braille"] = {
        "display": _ci_get(s_braille, "display"),
        "port": _ci_get(s_braille, "port"),
        "inputTable": _ci_get(s_braille, "inputTable"),
        "outputTable": _ci_get(s_braille, "outputTable"),
        "tetherTo": _ci_get(s_braille, "tetherTo"),
        "showCursor": _to_bool(_ci_get(s_braille, "showCursor")),
        "cursorBlinkRate": _to_int(_ci_get(s_braille, "cursorBlinkRate")),
        "expandToComputerBraille": _to_bool(_ci_get(s_braille, "expandToComputerBraille")),
    }

    # audio
    out["audio"] = {
        "audioDuckingMode": _ci_get(s_audio, "audioDuckingMode"),
        "beepSpeechMode": _ci_get(s_audio, "beepSpeechMode"),
        "outputDevice": _ci_get(s_audio, "outputDevice"),
    }

    # vision
    out["vision"] = {
        "enabledProviders": _ci_get(s_vision, "enabledProviders"),
        "screenCurtain": _to_bool(_ci_get(s_vision, "screenCurtain")),
        "focusHighlight": _to_bool(_ci_get(s_vision, "focusHighlight")),
        "caretHighlight": _to_bool(_ci_get(s_vision, "caretHighlight")),
        "hoverHighlight": _to_bool(_ci_get(s_vision, "hoverHighlight")),
    }

    # keyboard
    out["keyboard"] = {
        "keyboardLayout": _ci_get(s_kbd, "keyboardLayout"),
        "useCapsLockAsNVDAModifierKey": _to_bool(_ci_get(s_kbd, "useCapsLockAsNVDAModifierKey")),
        "useNumpadInsertAsNVDAModifierKey": _to_bool(_ci_get(s_kbd, "useNumpadInsertAsNVDAModifierKey")),
        "useExtendedInsertAsNVDAModifierKey": _to_bool(_ci_get(s_kbd, "useExtendedInsertAsNVDAModifierKey")),
        "speakTypedCharacters": _to_bool(_ci_get(s_kbd, "speakTypedCharacters")),
        "speakTypedWords": _to_bool(_ci_get(s_kbd, "speakTypedWords")),
        "speakCommandKeys": _to_bool(_ci_get(s_kbd, "speakCommandKeys")),
    }

    # mouse
    out["mouse"] = {
        "enableMouseTracking": _to_bool(_ci_get(s_mouse, "enableMouseTracking")),
        "reportObjectUnderMouse": _to_bool(_ci_get(s_mouse, "reportObjectUnderMouse")),
        "audioCoordinatesEnable": _to_bool(_ci_get(s_mouse, "audioCoordinatesEnable")),
    }

    # doc/object formatting — include whatever is present
    out["documentFormatting"] = {
        k: (_to_bool(v) if _to_bool(v) is not None else v) for k, v in s_doc.items()
    }
    out["objectPresentation"] = {
        k: (_to_bool(v) if _to_bool(v) is not None else v) for k, v in s_obj.items()
    }

    # profile overrides (raw)
    prof_dir = root / "profiles"
    if prof_dir.is_dir():
        for prof_ini in prof_dir.glob("*.ini"):
            pcfg = _read_ini_any_encoding(prof_ini)
            body = {}
            for sec in ("speech","braille","audio","vision","keyboard","mouse","documentFormatting","objectPresentation"):
                s = _ci_section(pcfg, sec)
                if s:
                    body[sec] = s
            if body:
                out["profiles_overrides"][prof_ini.stem] = body

    return out

# ---------------------- JAWS helpers ----------------------

# --- add these helpers near JAWS helpers ---

def _reg_enum_tree(root, subkey, max_depth=6):
    """
    Enumerate registry tree values (names only, with a few values) to help us find where things live.
    Returns {"_values": {name: str(val)}, "_children": {sub: {...}}}
    """
    node = {"_values": {}, "_children": {}}
    try:
        with winreg.OpenKey(root, subkey) as k:
            # values
            i = 0
            while True:
                try:
                    name, val, _typ = winreg.EnumValue(k, i)
                    node["_values"][name] = str(val)
                    i += 1
                except OSError:
                    break
            # children
            if max_depth > 0:
                j = 0
                while True:
                    try:
                        child = winreg.EnumKey(k, j); j += 1
                        child_path = f"{subkey}\\{child}"
                        node["_children"][child] = _reg_enum_tree(root, child_path, max_depth - 1)
                    except OSError:
                        break
    except OSError:
        pass
    return node

def _reg_search_first_value(roots_and_subkeys, want_names, max_depth=6):
    """
    Depth-first search for the first (most local) occurrence of any value with a name in want_names.
    Tries both 64/32-bit views automatically via _reg_read_views calls on each node.
    """
    seen = set()
    want_lc = {w.lower() for w in want_names}

    def _walk(root, sub):
        key_id = (root, sub)
        if key_id in seen:
            return None
        seen.add(key_id)

        # check local values in both views
        for view in [getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0), 0]:
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as k:
                    i = 0
                    found_here = {}
                    while True:
                        try:
                            name, val, _typ = winreg.EnumValue(k, i)
                            if name.lower() in want_lc and name not in found_here:
                                found_here[name] = str(val)
                            i += 1
                        except OSError:
                            break
                    if found_here:
                        return found_here
                    # recurse
                    if max_depth > 0:
                        j = 0
                        while True:
                            try:
                                child = winreg.EnumKey(k, j); j += 1
                                res = _walk(root, f"{sub}\\{child}")
                                if res:
                                    return res
                            except OSError:
                                break
            except OSError:
                pass
        return None

    for root, sub in roots_and_subkeys:
        res = _walk(root, sub)
        if res:
            return res
    return None

def collect_jaws_settings_curated():
    """
    Compact, user-meaningful JAWS settings by consulting registry in both views.
    If direct paths are empty, recursively search for common names.
    """
    running = any((p.info["name"] or "").lower() in {"jfw.exe", "fsdom.exe"} for p in psutil.process_iter(["name"]))
    user_dir = _jaws_user_settings_dir()
    shared_dir = _jaws_shared_settings_dir()
    ver = _jaws_latest_version_in_registry()
    base = f"Software\\Freedom Scientific\\JAWS\\{ver}" if ver else None
    base_wow = f"Software\\WOW6432Node\\Freedom Scientific\\JAWS\\{ver}" if ver else None

    out = {
        "present": bool(user_dir or ver),
        "running": running,
        "version": ver,
        "sources": {
            "registry_base": base,
            "user_dir": str(user_dir) if user_dir else None,
            "shared_dir": str(shared_dir) if shared_dir else None,
        },
        "speech": {},
        "braille": {},
        "keyboard": {},
        "audio": {},
        "vision": {},
        "other": {},
        "_debug": {}  # <--- small debug
    }
    if not (ver and (base or base_wow)):
        return out

    # candidate subkeys (HKCU/HKLM × base/base_wow)
    opts = []
    synth = []
    bra  = []
    kbd  = []
    vc   = []
    for b in filter(None, (base, base_wow)):
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            opts.append((root, f"{b}\\Options"))
            synth.append((root, f"{b}\\Synthesizer"))
            bra.append((root, f"{b}\\Braille"))
            kbd.append((root, f"{b}\\Keyboard"))
            vc.append((root, f"{b}\\VirtualCursor"))

    # Direct reads
    def P(pairs, name): return [(root, sub, name) for (root, sub) in pairs]

    speech = {
        "currentSynth": _reg_try(P(synth, "CurrentSynth") + P(opts, "CurrentSynth") + P(opts, "Synthesizer")),
        "rate": _reg_try(P(opts, "Rate") + P(opts, "SpeechRate") + P(opts, "SayAllSpeed") + P(opts, "Speed")),
        "pitch": _reg_try(P(opts, "Pitch")),
        "volume": _reg_try(P(opts, "Volume") + P(opts, "MainVolume")),
        "punctuation": _reg_try(P(opts, "Punctuation") + P(opts, "PunctuationLevel") + P(opts, "PunctLevel")),
        "typingEcho": _reg_try(P(opts, "TypingEcho") + P(opts, "KeyboardEcho")),
        "sayAllMode": _reg_try(P(opts, "SayAllMode")),
        "language": _reg_try(P(opts, "Language")),
        "voiceProfile": _reg_try(P(opts, "VoiceProfile") + P(opts, "ActiveVoiceProfile")),
    }

    braille = {
        "display": _reg_try(P(bra, "Display") + P(bra, "BrailleDisplay")),
        "port": _reg_try(P(bra, "Port")),
        "inputTable": _reg_try(P(bra, "InputTable")),
        "outputTable": _reg_try(P(bra, "OutputTable")),
        "tether": _reg_try(P(bra, "Tether")),
        "showCursor": _reg_try(P(bra, "ShowCursor")),
        "blinkRate": _reg_try(P(bra, "BlinkRate")),
    }

    keyboard = {
        "layout": _reg_try(P(kbd, "Layout")),
        "tutorMessages": _reg_try(P(opts, "TutorMessages")),
        "screenEcho": _reg_try(P(opts, "ScreenEcho")),
        "mouseEcho": _reg_try(P(opts, "MouseEcho")),
        "keyRepeat": _reg_try(P(kbd, "KeyRepeat")),
    }

    audio = {
        "audioDucking": _reg_try(P(opts, "AudioDucking")),
        "beepOnCaps": _reg_try(P(opts, "BeepOnCapsLock")),
        "beepOnScroll": _reg_try(P(opts, "BeepOnScroll")),
    }

    vision = {
        "useVirtualCursor": _reg_try(P(vc, "Enabled") + P(opts, "VirtualCursor")),
        "documentNavigationOrder": _reg_try(P(vc, "DocumentNavOrder")),
        "screenShades": _reg_try(P(opts, "ScreenShades")),
        "highlight": _reg_try(P(opts, "Highlight")),
    }

    # If some speech fields are still None, search the whole tree for common names once.
    if any(v is None for v in speech.values()):
        want = {"CurrentSynth","Rate","SpeechRate","SayAllSpeed","Speed","Pitch","Volume","MainVolume",
                "Punctuation","PunctuationLevel","PunctLevel","TypingEcho","KeyboardEcho","SayAllMode","Language",
                "VoiceProfile","ActiveVoiceProfile"}
        roots = []
        for b in filter(None, (base, base_wow)):
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                roots.append((root, b))
        found = _reg_search_first_value(roots, want, max_depth=6) or {}
        # fill any missing
        for k in list(speech.keys()):
            if speech[k] is None:
                # try direct name or any alias
                aliases = {
                    "rate": ["Rate","SpeechRate","SayAllSpeed","Speed"],
                    "volume": ["Volume","MainVolume"],
                    "punctuation": ["Punctuation","PunctuationLevel","PunctLevel"],
                    "typingEcho": ["TypingEcho","KeyboardEcho"],
                    "voiceProfile": ["VoiceProfile","ActiveVoiceProfile"],
                    "currentSynth": ["CurrentSynth","Synthesizer"],
                }.get(k, [k])
                for nm in aliases:
                    for fk, fv in found.items():
                        if fk.lower() == nm.lower():
                            speech[k] = fv; break
                    if speech[k] is not None:
                        break

    out["speech"] = speech
    out["braille"] = braille
    out["keyboard"] = keyboard
    out["audio"] = audio
    out["vision"] = vision

    # minimal debug: show whether the obvious subkeys exist & a tiny tree for HKCU base
    try:
        if base:
            out["_debug"]["HKCU_base_tree_sample"] = { "path": base, "sample": _reg_enum_tree(winreg.HKEY_CURRENT_USER, base, max_depth=2) }
    except Exception:
        pass

    return out


def _reg_read_views(root, subkey, name):
    """
    Read a registry value trying both 64-bit and 32-bit views for HKLM/HKCU.
    Returns str(value) or None.
    """
    views = [0]
    # On 64-bit Windows, also try the alternate view
    try:
        views = [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]
    except Exception:
        pass

    for view in views:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as k:
                v, _ = winreg.QueryValueEx(k, name)
                return str(v)
        except OSError:
            continue
        except Exception:
            continue
    return None

def _reg_try(paths_and_names):
    """
    Try multiple (root, subkey, valueName) tuples across views.
    """
    for root, sub, name in paths_and_names:
        v = _reg_read_views(root, sub, name)
        if v is not None:
            return v
    return None

def _jaws_user_settings_dir():
    appdata = os.environ.get("APPDATA") or ""
    base = Path(appdata) / "Freedom Scientific" / "JAWS"
    if not base.is_dir(): return None
    pairs = []
    for child in base.iterdir():
        m = re.findall(r"\d+", child.name)
        if m and (child / "Settings" / "enu").is_dir():
            pairs.append(([int(x) for x in m], child))
    if not pairs: return None
    _, newest = sorted(pairs)[-1]
    return newest / "Settings" / "enu"

def _jaws_shared_settings_dir():
    progdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    base = Path(progdata) / "Freedom Scientific" / "JAWS"
    if not base.is_dir(): return None
    pairs = []
    for child in base.iterdir():
        m = re.findall(r"\d+", child.name)
        if m and (child / "SETTINGS" / "enu").is_dir():
            pairs.append(([int(x) for x in m], child))
    if not pairs: return None
    _, newest = sorted(pairs)[-1]
    return newest / "SETTINGS" / "enu"

def _jaws_latest_version_in_registry():
    """
    Look for the highest numeric subkey under HKCU\Software\Freedom Scientific\JAWS
    and HKCU\Software\WOW6432Node\Freedom Scientific\JAWS.
    """
    candidates = []

    def _enum_versions(root, base_sub):
        try:
            with winreg.OpenKey(root, base_sub) as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i); i += 1
                        if re.search(r"\d", sub):
                            candidates.append(sub)
                    except OSError:
                        break
        except Exception:
            pass

    _enum_versions(winreg.HKEY_CURRENT_USER, r"Software\Freedom Scientific\JAWS")
    _enum_versions(winreg.HKEY_CURRENT_USER, r"Software\WOW6432Node\Freedom Scientific\JAWS")

    if not candidates:
        return None
    def ver_key(s): return [int(x) for x in re.findall(r"\d+", s)]
    return sorted(candidates, key=ver_key)[-1]

def collect_jaws_settings_curated():
    """
    Compact, user-meaningful JAWS settings by consulting registry in both views.
    Falls back to nothing if not found (JCF is binary; not parsed here).
    """
    running = any((p.info["name"] or "").lower() in {"jfw.exe", "fsdom.exe"} for p in psutil.process_iter(["name"]))
    user_dir = _jaws_user_settings_dir()
    shared_dir = _jaws_shared_settings_dir()
    ver = _jaws_latest_version_in_registry()
    base = f"Software\\Freedom Scientific\\JAWS\\{ver}" if ver else None
    base_wow = f"Software\\WOW6432Node\\Freedom Scientific\\JAWS\\{ver}" if ver else None

    out = {
        "present": bool(user_dir or ver),
        "running": running,
        "version": ver,
        "sources": {
            "registry_base": base,
            "user_dir": str(user_dir) if user_dir else None,
            "shared_dir": str(shared_dir) if shared_dir else None,
        },
        "speech": {},
        "braille": {},
        "keyboard": {},
        "audio": {},
        "vision": {},
        "other": {}
    }
    if not (ver and (base or base_wow)):
        return out

    # Build candidate subkeys for both views and WOW6432Node
    opts = []
    synth = []
    bra  = []
    kbd  = []
    vc   = []
    for b in filter(None, (base, base_wow)):
        opts.append((winreg.HKEY_CURRENT_USER, f"{b}\\Options"))
        synth.append((winreg.HKEY_CURRENT_USER, f"{b}\\Synthesizer"))
        bra.append((winreg.HKEY_CURRENT_USER, f"{b}\\Braille"))
        kbd.append((winreg.HKEY_CURRENT_USER, f"{b}\\Keyboard"))
        vc.append((winreg.HKEY_CURRENT_USER, f"{b}\\VirtualCursor"))
        # Also try HKLM as a fallback (defaults)
        opts.append((winreg.HKEY_LOCAL_MACHINE, f"{b}\\Options"))
        synth.append((winreg.HKEY_LOCAL_MACHINE, f"{b}\\Synthesizer"))
        bra.append((winreg.HKEY_LOCAL_MACHINE, f"{b}\\Braille"))
        kbd.append((winreg.HKEY_LOCAL_MACHINE, f"{b}\\Keyboard"))
        vc.append((winreg.HKEY_LOCAL_MACHINE, f"{b}\\VirtualCursor"))

    # Helper to form path/name tuples for _reg_try
    def P(pairs, name):
        return [(root, sub, name) for (root, sub) in pairs]

    # Speech
    out["speech"] = {
        "currentSynth": _reg_try(P(synth, "CurrentSynth") + P(opts, "CurrentSynth") + P(opts, "Synthesizer")),
        # JAWS uses various names across versions for rate/volume/pitch
        "rate": _reg_try(P(opts, "Rate") + P(opts, "SpeechRate") + P(opts, "SayAllSpeed") + P(opts, "Speed")),
        "pitch": _reg_try(P(opts, "Pitch")),
        "volume": _reg_try(P(opts, "Volume") + P(opts, "MainVolume")),
        "punctuation": _reg_try(P(opts, "Punctuation") + P(opts, "PunctuationLevel") + P(opts, "PunctLevel")),
        "typingEcho": _reg_try(P(opts, "TypingEcho") + P(opts, "KeyboardEcho")),
        "sayAllMode": _reg_try(P(opts, "SayAllMode")),
        "language": _reg_try(P(opts, "Language")),
        "voiceProfile": _reg_try(P(opts, "VoiceProfile") + P(opts, "ActiveVoiceProfile")),
    }

    # Braille
    out["braille"] = {
        "display": _reg_try(P(bra, "Display") + P(bra, "BrailleDisplay")),
        "port": _reg_try(P(bra, "Port")),
        "inputTable": _reg_try(P(bra, "InputTable")),
        "outputTable": _reg_try(P(bra, "OutputTable")),
        "tether": _reg_try(P(bra, "Tether")),
        "showCursor": _reg_try(P(bra, "ShowCursor")),
        "blinkRate": _reg_try(P(bra, "BlinkRate")),
    }

    # Keyboard
    out["keyboard"] = {
        "layout": _reg_try(P(kbd, "Layout")),
        "tutorMessages": _reg_try(P(opts, "TutorMessages")),
        "screenEcho": _reg_try(P(opts, "ScreenEcho")),
        "mouseEcho": _reg_try(P(opts, "MouseEcho")),
        "keyRepeat": _reg_try(P(kbd, "KeyRepeat")),
    }

    # Audio
    out["audio"] = {
        "audioDucking": _reg_try(P(opts, "AudioDucking") + P((bra + opts), "AudioDucking")),
        "beepOnCaps": _reg_try(P(opts, "BeepOnCapsLock")),
        "beepOnScroll": _reg_try(P(opts, "BeepOnScroll")),
    }

    # Vision / virtual cursor
    out["vision"] = {
        "useVirtualCursor": _reg_try(P(vc, "Enabled") + P(opts, "VirtualCursor")),
        "documentNavigationOrder": _reg_try(P(vc, "DocumentNavOrder")),
        "screenShades": _reg_try(P(opts, "ScreenShades")),
        "highlight": _reg_try(P(opts, "Highlight")),
    }

    return out

# ---------------------- combine ----------------------

def collect_screen_reader_settings():
    return {
        "nvda": collect_nvda_settings_curated(),
        "jaws": collect_jaws_settings_curated(),
    }

# ---------------------- generic Windows a11y state + diff ----------------------

class _HIGHCONTRASTW:
    # simple ctypes-less proxy to avoid hard dependency here; implemented by caller if needed
    pass

def get_accessibility_state():
    """
    A small, stable set of Windows a11y toggles. (Same shape you used before)
    """
    # Because this module is imported by apps that already have ctypes helpers,
    # re-implement a minimal subset without hard ctypes. You can override in main.
    import ctypes
    class HIGHCONTRASTW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint),
                    ("dwFlags", ctypes.c_uint),
                    ("lpszDefaultScheme", ctypes.c_wchar_p)]
    SPI_GETHIGHCONTRAST = 0x0042
    HCF_HIGHCONTRASTON = 0x0001

    def _get_high_contrast_enabled():
        try:
            hc = HIGHCONTRASTW()
            hc.cbSize = ctypes.sizeof(HIGHCONTRASTW)
            res = ctypes.windll.user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, hc.cbSize, ctypes.byref(hc), 0)
            if res:
                return bool(hc.dwFlags & HCF_HIGHCONTRASTON)
        except Exception:
            pass
        return None

    def _read_reg(root, path, name):
        try:
            with winreg.OpenKey(root, path) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return val
        except Exception:
            return None

    def _proc_running(fragment):
        frag = (fragment or "").lower()
        for p in psutil.process_iter(["name"]):
            try:
                nm = (p.info["name"] or "").lower()
                if frag and frag in nm:
                    return True
            except Exception:
                pass
        return False

    narrator_running = _proc_running("narrator.exe")
    narrator_startup = bool(_read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Narrator\NoRoam", "WinEnterLaunchEnabled"))

    mag_zoom = _read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ScreenMagnifier", "Magnification")
    magnifier_running = _proc_running("magnify.exe")

    cf_active = _read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ColorFiltering", "Active")
    cf_type_raw = _read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ColorFiltering", "FilterType")
    FILTER_TYPES = {0: "none", 1: "inverted", 2: "grayscale", 3: "red-green", 4: "green-red", 5: "blue-yellow"}
    try:
        cf_type = FILTER_TYPES.get(int(cf_type_raw), "unknown")
    except Exception:
        cf_type = "unknown"
    cf_enabled = bool(int(cf_active)) if str(cf_active).isdigit() else False

    hc_enabled = _get_high_contrast_enabled()
    hc_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\HighContrast", "Flags")

    sk_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\StickyKeys", "Flags")
    tk_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\ToggleKeys", "Flags")
    fk_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\Keyboard Response", "Flags")

    scaling = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "LogPixels")
    font_smoothing = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "FontSmoothing")
    arrow = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors", "Arrow")
    if isinstance(arrow, str) and "aero" in arrow.lower():
        cursor_scheme = "windows aero"
    elif isinstance(arrow, str) and "windows black" in arrow.lower():
        cursor_scheme = "windows black"
    else:
        cursor_scheme = os.path.basename(arrow) if isinstance(arrow, str) else "unavailable"

    def _nz(x):
        try:
            return int(x) != 0
        except Exception:
            return False

    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "narrator": {"enabled": narrator_running, "startup_enabled": narrator_startup},
        "magnifier": {"enabled": magnifier_running, "zoom": int(mag_zoom) if str(mag_zoom).isdigit() else mag_zoom},
        "color_filter": {"enabled": cf_enabled, "type": cf_type},
        "high_contrast": {"enabled": hc_enabled, "flags": str(hc_flags) if hc_flags is not None else "unavailable"},
        "sticky_keys": {"flags": str(sk_flags) if sk_flags is not None else "unavailable", "maybe_enabled": _nz(sk_flags)},
        "toggle_keys": {"flags": str(tk_flags) if tk_flags is not None else "unavailable", "maybe_enabled": _nz(tk_flags)},
        "filter_keys": {"flags": str(fk_flags) if fk_flags is not None else "unavailable", "maybe_enabled": _nz(fk_flags)},
        "font_smoothing": font_smoothing,
        "display_scaling": scaling,
        "mouse_cursor_scheme": cursor_scheme,
    }

def diff_accessibility(prev, curr):
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return None
    def pick(d, path, default=None):
        try:
            for k in path:
                d = d[k]
            return d
        except Exception:
            return default
    checks = [
        (("narrator", "enabled"),),
        (("narrator", "startup_enabled"),),
        (("magnifier", "enabled"),),
        (("magnifier", "zoom"),),
        (("color_filter", "enabled"),),
        (("color_filter", "type"),),
        (("high_contrast", "enabled"),),
        (("sticky_keys", "maybe_enabled"),),
        (("toggle_keys", "maybe_enabled"),),
        (("filter_keys", "maybe_enabled"),),
    ]
    changes = {}
    for (p,) in checks:
        old = pick(prev, p)
        new = pick(curr, p)
        if old != new:
            changes["/".join(p)] = {"old": old, "new": new}
    return changes or None
