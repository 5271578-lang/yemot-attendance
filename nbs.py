"""Calls to the NBS / call2all API (outgoing calls and recording downloads).

Documentation: https://support.nbs-app.net/post/3465
Only voice calls are used - never SMS.
"""
import json
import os

import requests

import db

API = os.environ.get("NBS_API_URL", "https://www.call2all.co.il/ym/api/").rstrip("/") + "/"
TOKEN = os.environ.get("NBS_TOKEN", "")              # "systemnumber:password" or an API key
TEMPLATE_ID = os.environ.get("NBS_TEMPLATE_ID", "")   # optional: campaign whose redial settings to use
CALLER_ID = os.environ.get("NBS_CALLER_ID", "")       # optional outgoing caller id


def enabled():
    return bool(TOKEN)


def israeli(phone):
    p = db.normalize_phone(phone)
    return p if p.startswith("0") and len(p) in (9, 10) else None


def _get(command, timeout=20, **params):
    params["token"] = TOKEN
    r = requests.get(API + command, params=params, timeout=timeout)
    r.raise_for_status()
    return r


REDIAL_WAIT_SECONDS = 90


def redial_template(attempts, wait=None, kind="shuttle"):
    """Campaign template that dials each number up to `attempts` times (`wait` seconds apart), stopping once
    answered. Created on first use and remembered. Returns its id, or None.
    kind "forward" = separate templates whose voice file is a forwarded message (never used for other calls)."""
    wait = wait or REDIAL_WAIT_SECONDS
    key = (f"nbs_template_{attempts}" if kind == "shuttle" else f"nbs_{kind}_template_{attempts}") + \
        ("" if wait == REDIAL_WAIT_SECONDS else f"_{wait}")
    tid = db.get_setting(key)
    if tid:
        return tid
    name = f"attendance-{kind}-{attempts}" + ("" if wait == REDIAL_WAIT_SECONDS else f"-{wait}s")
    try:
        templates = _get("GetTemplates").json().get("templates") or []
        tid = next((str(t["templateId"]) for t in templates if t.get("description") == name), None)
        if not tid:
            tid = str(_get("CreateTemplate", description=name).json()["templateId"])
        ok = _get("UpdateTemplate", templateId=tid, maxDialAttempts=str(attempts), redialWait=str(wait),
                  redialPolicy="FAILED").json().get("responseStatus") == "OK"
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    if ok:
        db.set_setting(key, tid)
        return tid
    return None


def run_tts_calls_with_attempts(texts_by_phone, attempts_by_phone):
    """Like run_tts_calls, but each phone gets its own number of attempts (1-5)."""
    groups = {}
    for phone, txt in texts_by_phone.items():
        groups.setdefault(attempts_by_phone.get(phone) or 1, {})[phone] = txt
    results = [run_tts_calls(texts, template_id=redial_template(n) if n > 1 else None)
               for n, texts in sorted(groups.items())]
    ok = all(r[0] for r in results)
    return ok, ", ".join(r[1] for r in results)


def run_tts_calls(texts_by_phone, template_id=None):
    """Phone each number and read it its own text. Returns (ok, campaign_id_or_error)."""
    phones = {israeli(p): t for p, t in texts_by_phone.items() if israeli(p)}
    if not phones:
        return False, "no valid phones"
    if not enabled():
        return False, "NBS_TOKEN not set"
    params = {"ttsMode": "1", "phones": json.dumps(phones, ensure_ascii=False)}
    if template_id or TEMPLATE_ID:
        params["templateId"] = template_id or TEMPLATE_ID
    if CALLER_ID:
        params["callerId"] = CALLER_ID
    try:
        data = _get("RunCampaign", **params).json()
    except (requests.RequestException, ValueError) as ex:
        return False, f"error: {ex}"
    if data.get("responseStatus") != "OK":
        return False, data.get("message") or data.get("responseStatus") or "error"
    return True, str(data.get("campaignId") or data.get("campaign_id") or "sent")


TZINTUK_SECONDS = 16


def run_tzintuk(phones):
    """Ring each number briefly (a 'missed call'), no message. Returns (ok, info)."""
    nums = [p for p in (israeli(x) for x in phones) if p]
    if not nums:
        return False, "no valid phones"
    if not enabled():
        return False, "NBS_TOKEN not set"
    params = {"phones": ":".join(nums), "TzintukTimeOut": str(TZINTUK_SECONDS)}
    if CALLER_ID:
        params["callerId"] = CALLER_ID
    try:
        data = _get("RunTzintuk", **params).json()
    except (requests.RequestException, ValueError) as ex:
        return False, f"error: {ex}"
    if str(data.get("responseStatus", "")).upper() != "OK":
        return False, data.get("message") or data.get("responseStatus") or "error"
    return True, str(data.get("campaignId") or data.get("callsCount") or "sent")


def set_redial(max_attempts=3, wait_seconds=120):
    """Configure the campaign template to retry unanswered calls."""
    if not (enabled() and TEMPLATE_ID):
        return False, "NBS_TOKEN and NBS_TEMPLATE_ID are required"
    try:
        data = _get("UpdateTemplate", templateId=TEMPLATE_ID, maxDialAttempts=str(max_attempts),
                    redialWait=str(wait_seconds), redialPolicy="FAILED").json()
    except (requests.RequestException, ValueError) as ex:
        return False, str(ex)
    return data.get("responseStatus") == "OK", data.get("message") or data.get("responseStatus")


def urgent_calls(texts_by_phone, wait=15 * 60, tries=(10, 8, 5, 3)):
    """Calls that are redialled every `wait` seconds until answered - as many times as the system allows
    (tries the largest number first). Returns (ok, info)."""
    for n in tries:
        tid = redial_template(n, wait)
        if tid:
            ok, info = run_tts_calls(texts_by_phone, template_id=tid)
            return ok, f"{info} (up to {n} calls, every {wait // 60} minutes)"
    return run_tts_calls(texts_by_phone)


def join_wavs(parts):
    """Several .wav recordings one after the other (same format), or the first one if they cannot be joined."""
    import io
    import wave
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return parts[0] if parts else None
    try:
        out = io.BytesIO()
        with wave.open(io.BytesIO(parts[0])) as w0:
            params = w0.getparams()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            for p in parts:
                with wave.open(io.BytesIO(p)) as wi:
                    w.writeframes(wi.readframes(wi.getnframes()))
        return out.getvalue()
    except Exception:
        return parts[0]


def _ok(r):
    try:
        d = r.json()
    except ValueError:
        return False
    return str(d.get("responseStatus", "")).upper() == "OK" and d.get("success", True) is not False


def set_template_voice(tid, wav, folder):
    """Make `wav` the voice message of campaign template `tid`: upload it next to the recordings, copy it to
    tpl:<id> (FileAction), remove the copy. If that fails, upload straight to tpl:<id>."""
    tmp = f"{folder.rstrip('/')}/forward_{tid}.wav"
    try:
        up = requests.post(API + "UploadFile", data={"token": TOKEN, "path": f"ivr2:{tmp}", "convertAudio": "1"},
                           files={"file": ("forward.wav", wav, "audio/wav")}, timeout=60)
        if _ok(up) and _ok(_get("FileAction", action="copy", what0=f"ivr2:{tmp}", target=f"tpl:{tid}")):
            _get("FileAction", action="delete", what0=f"ivr2:{tmp}")
            return True
        up = requests.post(API + "UploadFile", data={"token": TOKEN, "path": f"tpl:{tid}", "convertAudio": "1"},
                           files={"file": ("forward.wav", wav, "audio/wav")}, timeout=60)
        return _ok(up)
    except requests.RequestException:
        return False


def voice_calls(wav, texts_by_phone, folder, wait=15 * 60, tries=(10, 8, 5, 3)):
    """Phone everyone and play them the recording itself when they answer (then each one's own short text).
    Unanswered numbers are dialled again every `wait` seconds, as many times as the system allows."""
    phones = {israeli(p): t for p, t in texts_by_phone.items() if israeli(p)}
    if not (enabled() and wav and phones):
        return False, "nothing to send"
    for n in tries:
        tid = redial_template(n, wait, kind="forward")
        if not tid:
            continue
        if not set_template_voice(tid, wav, folder):
            return False, "could not upload the recording"
        params = {"templateId": tid, "ttsMode": "1", "phones": json.dumps(phones, ensure_ascii=False)}
        if CALLER_ID:
            params["callerId"] = CALLER_ID
        try:
            data = _get("RunCampaign", **params).json()
        except (requests.RequestException, ValueError) as ex:
            return False, f"error: {ex}"
        if data.get("responseStatus") != "OK":
            return False, data.get("message") or data.get("responseStatus") or "error"
        return True, f"{data.get('campaignId') or 'sent'} (up to {n} calls, every {wait // 60} minutes)"
    return False, "no redial template"


def download_recording(path):
    """Returns the .wav bytes of a recording saved on the phone system, or None."""
    if not enabled():
        return None
    try:
        return _get("DownloadFile", timeout=30, path=f"ivr2:{path}.wav").content
    except requests.RequestException:
        return None
