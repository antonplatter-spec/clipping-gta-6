"""Bridge ops of the API server, registered into automate.bridge.OPS (loaded when the bridge doesn't know an op).

  fish_key_check  {} -> {ok, message}   tests the Fish Audio key exactly like `clipper voice setup` does
                  (clipper.voice_setup.check_key: a one-word TTS request with the free model). The key comes from
                  FISH_API_KEY in the environment, which runtime.clipper_env fills from the dashboard's secret, so it
                  is never part of a payload or a command line. The message is German.
"""
import os
import re

from automate import bridge


def _german(ok, message):
    if ok:
        return "Der Schlüssel funktioniert."
    m = re.search(r"HTTP (\d+)", message or "")
    if m:
        code = int(m.group(1))
        why = " – der Schlüssel ist ungültig oder gesperrt." if code in (401, 403) else "."
        return f"Fish Audio hat den Schlüssel abgelehnt (HTTP {code}){why}"
    m = re.search(r"\((.*)\)", message or "")
    return f"Fish Audio ist nicht erreichbar ({m.group(1) if m else 'Netzwerkfehler'})."


def fish_key_check(payload, cfg):
    from clipper import voice_setup
    key = os.environ.get("FISH_API_KEY", "")
    if not key:
        return {"ok": False, "message": "Es ist kein Fish-Audio-Schlüssel gespeichert."}
    ok, message = voice_setup.check_key(key, cfg.get("fish_model") or "s2.1-pro-free")
    return {"ok": bool(ok), "message": _german(ok, message)}


bridge.OPS["fish_key_check"] = fish_key_check
