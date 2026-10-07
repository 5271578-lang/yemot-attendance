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


def run_tts_calls(texts_by_phone):
    """Phone each number and read it its own text. Returns (ok, campaign_id_or_error)."""
    phones = {israeli(p): t for p, t in texts_by_phone.items() if israeli(p)}
    if not phones:
        return False, "no valid phones"
    if not enabled():
        return False, "NBS_TOKEN not set"
    params = {"ttsMode": "1", "phones": json.dumps(phones, ensure_ascii=False)}
    if TEMPLATE_ID:
        params["templateId"] = TEMPLATE_ID
    if CALLER_ID:
        params["callerId"] = CALLER_ID
    try:
        data = _get("RunCampaign", **params).json()
    except (requests.RequestException, ValueError) as ex:
        return False, f"error: {ex}"
    if data.get("responseStatus") != "OK":
        return False, data.get("message") or data.get("responseStatus") or "error"
    return True, str(data.get("campaignId") or data.get("campaign_id") or "sent")


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


def download_recording(path):
    """Returns the .wav bytes of a recording saved on the phone system, or None."""
    if not enabled():
        return None
    try:
        return _get("DownloadFile", timeout=30, path=f"ivr2:{path}.wav").content
    except requests.RequestException:
        return None
