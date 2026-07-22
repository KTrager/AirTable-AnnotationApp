#!/usr/bin/env python3
# =============================================================================
#  AIRTABLE IMAGE ANNOTATOR
#
#  What this app does, in plain English:
#    - It shows the images that live in an attachment field of your Airtable
#      table (for example SHAREABLE IMAGES in the SKU'S table).
#    - You can draw on an image like in Excel: crop, arrows, circles, boxes,
#      lines, freehand pen and text, in different colors.
#    - When you press Save, TWO things are written back to Airtable:
#        1. "ANNOTATIONS DATA"  - an invisible text field with the drawing
#           recipe. This is what lets you come back months later, reopen the
#           image, and every arrow/box/text is still movable and editable.
#        2. "ANNOTATED IMAGES"  - a normal attachment field with a finished
#           ("flattened") copy of the picture, drawings baked in. Point
#           Documint / Page Designer / any print engine at THIS field.
#    - The original image is never touched.
#
#  How it runs: this file starts a tiny private web server on your own Mac
#  (nothing is public, it only listens to your computer) and opens the editor
#  in your normal browser. Close the browser tab + the terminal window to quit.
#
#  It needs no extra installs - only Python itself and an internet connection
#  for Airtable. (The small drawing library the page uses is kept as a
#  verified copy right next to this script, so it also loads offline.)
#
#  The page itself (everything you see and click) lives in annotator.html,
#  in the same folder as this file.
# =============================================================================

import json
import os
import re
import sys
import base64
import subprocess
import tempfile
import threading
import time
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- where this app keeps its settings ---------------------------------------
# The settings (your Airtable token and your last choices) live in a small
# file right next to this script, just like the Image Downloader app does it.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "annotator_settings.json")

# If you already use the Airtable Image Downloader, we borrow its token on the
# first run so you don't have to paste it twice.
DOWNLOADER_SETTINGS = os.path.join(
    os.path.dirname(APP_DIR), "Airtable Image Downloader", "downloader_settings.json"
)

# The two fields this app manages inside your table. If they don't exist yet
# the app offers to create them (your token needs the "schema.bases:write"
# permission for that - otherwise it shows you how to add them by hand).
JSON_FIELD = "ANNOTATIONS DATA"
IMG_FIELD = "ANNOTATED IMAGES"
# Pictures pasted INTO an annotation (say, a snippet of another photo) are too
# big for the text field, so they live as small files in a third hidden field:
ASSET_FIELD = "ANNOTATION ASSETS"

API = "https://api.airtable.com/v0"
CONTENT_API = "https://content.airtable.com/v0"

# Engine version, matched against the page's own stamp on load. The page
# (annotator.html) is re-read on every reload, but THIS file only takes
# effect after a restart - when the two drift apart, the page shows a plain
# "please restart the app" note instead of confusing errors. Bump BOTH
# stamps together whenever the API between them changes.
ENGINE_VERSION = "2026-07-18"


# =============================================================================
#  SETTINGS - load and save the little json file
# =============================================================================

def load_settings():
    settings = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings = json.load(f)
        os.chmod(SETTINGS_FILE, 0o600)   # tighten old installs too
    except Exception:
        pass
    # First run: borrow the token from the Image Downloader if we can.
    if not settings.get("token"):
        try:
            with open(DOWNLOADER_SETTINGS, "r", encoding="utf-8") as f:
                other = json.load(f)
            if other.get("token"):
                settings["token"] = other["token"]
        except Exception:
            pass
    return settings


def save_settings(settings):
    # written to a temp file first, then swapped in atomically - a crash at
    # the wrong moment must never corrupt the file that holds the token
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    try:
        os.chmod(tmp, 0o600)             # the token is a password - owner-only
    except Exception:
        pass
    os.replace(tmp, SETTINGS_FILE)


SETTINGS = load_settings()


# =============================================================================
#  TALKING TO AIRTABLE
# =============================================================================

class AirtableError(Exception):
    """Something Airtable did not like. Carries a human-friendly message."""
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def airtable(url, payload=None, method=None, timeout=60):
    """Send one request to Airtable and give back the parsed JSON answer.

    Airtable allows 5 requests per second per base and answers 429 when the
    app is too eager (easy to hit while a big search loads every page) - so a
    429 is waited out and retried quietly before anyone sees an error."""
    token = SETTINGS.get("token", "")
    if not token:
        raise AirtableError(401, "No Airtable token saved yet.")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in (1, 2, 3):
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(0.4 * attempt + 0.2)   # 0.6s, then 1.0s
                continue
            return _airtable_error(e)
        except urllib.error.URLError as e:
            raise AirtableError(0, "Could not reach Airtable - is the internet "
                                   "connection ok? (%s)" % e.reason)


def _airtable_error(e):
    """Translate one HTTP error from Airtable into a sentence a person can
    act on, then raise it. (Split out of airtable() so the retry loop above
    stays readable.)"""
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        pass
    if e.code in (401,):
        msg = "Airtable rejected the token. Check it is pasted correctly."
    elif e.code == 403:
        msg = ("Your token is not allowed to do this yet. Open "
               "airtable.com/create/tokens, edit your token, and make sure it has the "
               "scopes data.records:read, data.records:write, schema.bases:read and "
               "schema.bases:write, plus access to this base.")
    elif e.code == 404:
        msg = "Airtable could not find that base/table/record (or the token has no access to it)."
    elif e.code == 429:
        msg = ("Airtable asked the app to slow down and kept saying so even after "
               "a few polite retries. Wait a few seconds and try again.")
    elif e.code == 422:
        if "UNKNOWN_FIELD_NAME" in body:
            # A field the app is set to use was renamed/deleted in Airtable.
            # Our cached idea of the table is now wrong - forget it, so the
            # next look (the Refresh button, reopening Settings) is fresh.
            _meta_cache.clear()
            m = re.search(r'Unknown field name:\s*\\?"([^"\\]+)', body)
            msg = ('Airtable does not know a field called "%s" any more - it was '
                   'probably just renamed or deleted in Airtable. Press Refresh, '
                   'then open Settings and pick the field again under its new '
                   'name. Nothing is lost.' % (m.group(1) if m else "?"))
        else:
            msg = "Airtable did not accept the data: " + body[:300]
    else:
        msg = "Airtable error %s: %s" % (e.code, body[:300])
    raise AirtableError(e.code, msg)


# A small cache so we don't re-ask Airtable for the table list on every click.
_meta_cache = {}   # baseId -> (timestamp, tables-json)

def base_tables(base_id, fresh=False):
    """All tables (with their fields and views) of one base."""
    now = time.time()
    if not fresh and base_id in _meta_cache and now - _meta_cache[base_id][0] < 300:
        return _meta_cache[base_id][1]
    data = airtable("%s/meta/bases/%s/tables" % (API, base_id))
    _meta_cache[base_id] = (now, data)
    return data


def find_table(base_id, table_id):
    for t in base_tables(base_id).get("tables", []):
        if t["id"] == table_id:
            return t
    # Maybe our cached copy is stale (fields were just added) - look again.
    for t in base_tables(base_id, fresh=True).get("tables", []):
        if t["id"] == table_id:
            return t
    raise AirtableError(404, "Table not found in this base.")


def get_record(base_id, table_id, record_id):
    """Fetch one record, with all its fields, keyed by FIELD ID.

    Field ids (fld…) never change, field names do - people rename columns on
    a whim. Working in ids end to end is what makes renames harmless.
    (Airtable's single-record address does not accept a field filter - asking
    for specific fields there makes it fail - so we take the whole record and
    pick out what we need afterwards.)"""
    return airtable("%s/%s/%s/%s?returnFieldsByFieldId=true"
                    % (API, base_id, table_id, record_id))


def patch_record(base_id, table_id, record_id, fields):
    """Write fields (keyed by field id) to one record; the answer comes back
    keyed by field id too, so it can be reused as a fresh record."""
    return airtable("%s/%s/%s/%s" % (API, base_id, table_id, record_id),
                    {"fields": fields, "returnFieldsByFieldId": True},
                    method="PATCH")


def field_by_ref(table, ref):
    """A field, found by its id ("fld…") or by its current name.

    The app stores ids - rename-proof. Names are still accepted everywhere,
    because older saved settings and the button links people already built in
    Airtable carry names."""
    if not ref:
        return None
    for f in table["fields"]:
        if f["id"] == ref:
            return f
    for f in table["fields"]:
        if f["name"] == ref:
            return f
    return None


# key, canonical name, required type, a word its creation description carries
_APP_FIELD_KINDS = (
    ("json", JSON_FIELD, "multilineText", "recipe"),
    ("img", IMG_FIELD, "multipleAttachments", "finished"),
    ("asset", ASSET_FIELD, "multipleAttachments", "pasted"),
)

def app_fields(table):
    """The app's three managed fields on one table: {"json":…, "img":…,
    "asset":…}, each a field dict or None.

    Found by their canonical names first. Failing that, by the description
    the app stamps on them at creation ("Managed by the Image Annotator
    app…") - so even renaming THOSE fields in Airtable does not break
    anything."""
    out = {}
    for key, name, ftype, _word in _APP_FIELD_KINDS:
        out[key] = next((f for f in table["fields"]
                         if f["name"] == name and f["type"] == ftype), None)
    for key, _name, ftype, word in _APP_FIELD_KINDS:
        if out[key]:
            continue
        claimed = {f["id"] for f in out.values() if f}
        out[key] = next((f for f in table["fields"]
                         if f["id"] not in claimed and f["type"] == ftype
                         and "Managed by the Image Annotator app" in (f.get("description") or "")
                         and word in (f.get("description") or "")), None)
    return out


# =============================================================================
#  THE ACTUAL WORK - the things the buttons in the browser ask the server to do
# =============================================================================

def api_bases():
    """List every base the token can see, for the first dropdown."""
    bases, offset = [], None
    while True:
        url = API + "/meta/bases" + (("?offset=" + offset) if offset else "")
        data = airtable(url)
        bases.extend(data.get("bases", []))
        offset = data.get("offset")
        if not offset:
            break
    bases.sort(key=lambda b: b["name"].lower())
    return {"bases": [{"id": b["id"], "name": b["name"]} for b in bases]}


def api_tables(base_id):
    """Tables of a base, plus which fields are attachments / text, plus views.

    Fields are given as {id, name} pairs - the page shows the names and
    remembers the IDS, so renaming a column in Airtable changes nothing."""
    out = []
    for t in base_tables(base_id, fresh=True).get("tables", []):
        af = app_fields(t)
        managed = {f["id"] for f in (af["img"], af["asset"]) if f}
        json_id = af["json"]["id"] if af["json"] else ""
        pair = lambda f: {"id": f["id"], "name": f["name"]}
        out.append({
            "id": t["id"],
            "name": t["name"],
            "views": [{"id": v["id"], "name": v["name"]} for v in t.get("views", [])],
            "attachmentFields": [pair(f) for f in t["fields"]
                                 if f["type"] == "multipleAttachments"
                                 and f["id"] not in managed],
            "allFields": [pair(f) for f in t["fields"]],
            "textFields": [pair(f) for f in t["fields"]
                           if f["type"] in ("multilineText", "richText", "singleLineText")
                           and f["id"] != json_id],
            # single-select fields can drive the filter chips in the list's rail
            "selectFields": [{"id": f["id"], "name": f["name"],
                              "choices": [c["name"] for c in
                                          (f.get("options") or {}).get("choices", [])]}
                             for f in t["fields"] if f["type"] == "singleSelect"],
            # a "Last modified time" field, when the table has one, gives the
            # list honest "updated" dates that include edits made in Airtable
            "modifiedField": next((f["id"] for f in t["fields"]
                                   if f["type"] == "lastModifiedTime"), ""),
            # the app's three managed fields, under their CURRENT names
            "appFields": {k: (pair(v) if v else None) for k, v in af.items()},
            "hasJsonField": bool(af["json"]),
            "hasImgField": bool(af["img"]),
            "hasAssetField": bool(af["asset"]),
        })
    return {"tables": out}


def api_ensure_fields(base_id, table_id, only=""):
    """Make sure our helper fields exist; try to create what's missing.
    With `only` set to one field name, just that field is created - the
    settings dialog has one Create button per field."""
    table = find_table(base_id, table_id)
    af = app_fields(table)   # finds them by name OR by their creation stamp,
    have_json = bool(af["json"])       # so a renamed app field still counts
    have_img = bool(af["img"])
    have_asset = bool(af["asset"])
    created, problem = [], None
    url = "%s/meta/bases/%s/tables/%s/fields" % (API, base_id, table_id)

    def wants(name):
        return not only or only == name

    try:
        if not have_json and wants(JSON_FIELD):
            airtable(url, {"name": JSON_FIELD, "type": "multilineText",
                           "description": "Managed by the Image Annotator app - the editable drawing "
                                          "recipe for each annotated image. Please do not edit by hand."})
            created.append(JSON_FIELD)
            have_json = True
        if not have_img and wants(IMG_FIELD):
            airtable(url, {"name": IMG_FIELD, "type": "multipleAttachments",
                           "description": "Managed by the Image Annotator app - finished annotated "
                                          "pictures, ready for Documint and other print tools."})
            created.append(IMG_FIELD)
            have_img = True
        if not have_asset and wants(ASSET_FIELD):
            airtable(url, {"name": ASSET_FIELD, "type": "multipleAttachments",
                           "description": "Managed by the Image Annotator app - pictures pasted into "
                                          "annotations live here. Please do not edit by hand."})
            created.append(ASSET_FIELD)
            have_asset = True
        if created:
            base_tables(base_id, fresh=True)   # refresh our cached table list
    except AirtableError as e:
        problem = str(e)
    return {"hasJsonField": have_json, "hasImgField": have_img, "hasAssetField": have_asset,
            "created": created, "problem": problem}


def api_check_write(base_id, table_id, record_id):
    """A harmless save-nothing test, so we can warn about permissions early."""
    try:
        airtable("%s/%s/%s/%s" % (API, base_id, table_id, record_id),
                 {"fields": {}}, method="PATCH")
        return {"canWrite": True}
    except AirtableError as e:
        return {"canWrite": False, "problem": str(e)}


def read_annotations_map(record, json_field_id):
    """The ANNOTATIONS DATA field holds one JSON object per record:
       { "<attachment id of the original>": {drawing recipe...}, ... }
    `json_field_id` is that field's id on this table (records arrive keyed
    by field id)."""
    raw = record.get("fields", {}).get(json_field_id or "") or ""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def api_records(base_id, table_id, view_id, image_field, name_field,
                notes_field="", start_offset="", filter_field="", modified_field=""):
    """The records for the picture grid: name + every image + 'annotated?'.

    Up to 600 records per call; when the table holds more, the answer carries
    an "offset" and the page keeps loading from there as the list scrolls
    (instead of the old behaviour: silently stopping at 600)."""
    table = find_table(base_id, table_id)
    # Field references arrive as ids (the app's own storage) or as names
    # (older settings, button links) - resolve either to the field itself.
    # The image field is essential, so a missing one gets an explanation;
    # the name field is cosmetic and quietly falls back to the primary field.
    img_f = field_by_ref(table, image_field)
    if not img_f:
        raise AirtableError(400, "The image field \"%s\" doesn't exist in this table any "
                                 "more - it was probably deleted in Airtable (renames "
                                 "are harmless). Open Settings and pick the image field "
                                 "again." % image_field)
    name_f = field_by_ref(table, name_field) or table["fields"][0]
    notes_f = field_by_ref(table, notes_field)
    filter_f = field_by_ref(table, filter_field)
    mod_f = field_by_ref(table, modified_field)
    af = app_fields(table)
    json_id = af["json"]["id"] if af["json"] else ""
    wanted = [img_f["id"], name_f["id"]]
    wanted += [f["id"] for f in (notes_f, filter_f, mod_f) if f]
    if json_id:
        wanted.append(json_id)

    records, offset = [], (start_offset or None)
    while len(records) < 600:
        qs = ["pageSize=100", "returnFieldsByFieldId=true"]
        if view_id:
            qs.append("view=" + urllib.parse.quote(view_id))
        for f in wanted:
            qs.append("fields%5B%5D=" + urllib.parse.quote(f))
        if offset:
            qs.append("offset=" + urllib.parse.quote(offset))
        data = airtable("%s/%s/%s?%s" % (API, base_id, table_id, "&".join(qs)))
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break

    return {"records": [record_summary(r, img_f["id"], name_f["id"],
                                       notes_f["id"] if notes_f else "",
                                       filter_f["id"] if filter_f else "",
                                       mod_f["id"] if mod_f else "",
                                       json_id)
                        for r in records],
            "offset": offset}


def record_summary(r, image_field, name_field, notes_field="",
                   filter_field="", modified_field="", json_field=""):
    """Boil one record down to what the picture grid needs.
    All field parameters are FIELD IDS; the record is keyed by field id."""
    annots = read_annotations_map(r, json_field)
    images = []
    for att in r.get("fields", {}).get(image_field) or []:
        if not isinstance(att, dict) or not att.get("id"):
            continue
        thumbs = att.get("thumbnails") or {}
        e = annots.get(att["id"])
        if not isinstance(e, dict):
            e = {}
        images.append({
            "id": att["id"],
            "filename": att.get("filename", "image"),
            # thumbnail links from Airtable expire after a while - that is
            # fine here because the browser loads them right away.
            "thumb": (thumbs.get("large") or thumbs.get("small") or {}).get("url") or att.get("url"),
            "annotated": bool(e.get("recipeAttId") or e.get("fabric") or e.get("annotatedAttId")),
            "print": bool(e.get("print")),
        })
    name = r.get("fields", {}).get(name_field)
    if isinstance(name, (list, dict)):
        name = json.dumps(name)
    notes = ""
    if notes_field:
        raw = r.get("fields", {}).get(notes_field)
        if isinstance(raw, str):
            notes = raw.strip().replace("\n", "  ")
            if len(notes) > 400:
                notes = notes[:400] + "…"

    # the record's single-select value, when a filter field is chosen
    filter_value = ""
    if filter_field:
        fv = r.get("fields", {}).get(filter_field)
        if isinstance(fv, str):
            filter_value = fv

    # "updated" for the list, most honest source first: a Last modified time
    # field (sees edits made in Airtable) -> the app's own save stamps
    # (= last annotated here) -> the record's created time.
    updated, updated_source = "", ""
    if modified_field:
        mv = r.get("fields", {}).get(modified_field)
        if isinstance(mv, str) and mv:
            updated, updated_source = mv, "modified"
    if not updated:
        stamps = [e.get("updated") for e in annots.values()
                  if isinstance(e, dict) and isinstance(e.get("updated"), str)]
        if stamps:
            # "YYYY-MM-DD HH:MM:SS" strings - max() picks the latest
            updated, updated_source = max(stamps), "annotated"
    if not updated:
        updated, updated_source = r.get("createdTime", ""), "created"

    return {"id": r["id"], "name": str(name) if name is not None else "(no name)",
            "notes": notes, "images": images,
            "filterValue": filter_value,
            "updated": updated, "updatedSource": updated_source}


def api_single_record(base_id, table_id, record_id, image_field, name_field,
                      notes_field="", filter_field="", modified_field=""):
    """One record only - used when a link/button in Airtable opens the app
    straight on a specific record."""
    table = find_table(base_id, table_id)
    img_f = field_by_ref(table, image_field)
    if not img_f:
        raise AirtableError(400, "The image field \"%s\" doesn't exist in this table any "
                                 "more - it was probably deleted in Airtable (renames "
                                 "are harmless). Open Settings, or fix the button "
                                 "formula, and pick the image field again." % image_field)
    name_f = field_by_ref(table, name_field) or table["fields"][0]
    notes_f = field_by_ref(table, notes_field)
    filter_f = field_by_ref(table, filter_field)
    mod_f = field_by_ref(table, modified_field)
    af = app_fields(table)
    return record_summary(get_record(base_id, table_id, record_id),
                          img_f["id"], name_f["id"],
                          notes_f["id"] if notes_f else "",
                          filter_f["id"] if filter_f else "",
                          mod_f["id"] if mod_f else "",
                          af["json"]["id"] if af["json"] else "")


# Small cache of downloaded image bytes so re-opening an image is instant.
_img_cache = {}          # attachment id -> (bytes, content-type)
_img_cache_order = []

def api_image_bytes(base_id, table_id, record_id, image_field, att_id):
    """Download the ORIGINAL image through the server.

    Why through the server? Two reasons:
      - Airtable image links expire after a couple of hours, so we always ask
        Airtable for a fresh link first.
      - The browser only lets the editor export a finished picture if the
        image came from this same local server (a browser security rule).
    """
    if att_id in _img_cache:
        return _img_cache[att_id]
    table = find_table(base_id, table_id)
    fld = field_by_ref(table, image_field)
    if not fld:
        raise AirtableError(400, "The attachment field this picture lives in doesn't "
                                 "exist in the table any more - it was probably deleted "
                                 "in Airtable. Open Settings and pick the image field "
                                 "again.")
    record = get_record(base_id, table_id, record_id)
    att = next((a for a in record.get("fields", {}).get(fld["id"]) or []
                if a.get("id") == att_id), None)
    if not att:
        raise AirtableError(404, "That image is no longer on the record - it may have "
                                 "been changed in Airtable since the list was loaded. "
                                 "Press Refresh to reload the list.")
    req = urllib.request.Request(att["url"])
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type") or ""
    # iPhone photos often arrive as HEIC, which most browsers cannot show.
    # They are converted to JPEG on the fly - see heic_to_jpeg for how.
    name = (att.get("filename") or "").lower()
    if (name.endswith((".heic", ".heif"))
            or "heic" in ctype or "heif" in ctype
            or "heic" in (att.get("type") or "")):
        try:
            body = heic_to_jpeg(body)
            ctype = "image/jpeg"
        except Exception:
            pass   # if conversion fails, hand over the original and let the
                   # browser try (Safari can show HEIC by itself)
    # Airtable sometimes serves the file as anonymous "binary data" instead of
    # saying it is an image. The browser needs a real image type, so if the
    # label is unhelpful we use the attachment's own type, or simply look at
    # the first bytes of the file (every image format signs its files).
    if not ctype.startswith("image/"):
        ctype = att.get("type") or ""
    if not ctype.startswith("image/"):
        if body[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif body[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif body[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        elif body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            ctype = "image/webp"
        elif body[:2] == b"BM":
            ctype = "image/bmp"
        elif body[:1] in (b"{", b"["):
            ctype = "application/json"    # a stored drawing recipe
        else:
            ctype = "image/jpeg"   # best guess - browsers sniff too
    _img_cache[att_id] = (body, ctype)
    _img_cache_order.append(att_id)
    while len(_img_cache_order) > 12:          # keep at most 12 images in memory
        _img_cache.pop(_img_cache_order.pop(0), None)
    return body, ctype


def heic_to_jpeg(body):
    """Convert HEIC/HEIF photo bytes to JPEG so every browser can show them.

    On a Mac the built-in "sips" tool does it - nothing to install. On
    Windows (and Linux) the optional Pillow + pillow-heif packages are used
    instead, when they are installed. One-time, in a terminal:
        pip install pillow pillow-heif
    Without them the original bytes are handed over unchanged (the caller
    treats any failure here as "let the browser try")."""
    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.heic")
            dst = os.path.join(td, "out.jpg")
            with open(src, "wb") as f:
                f.write(body)
            subprocess.run(["sips", "-s", "format", "jpeg",
                            "-s", "formatOptions", "90", src, "--out", dst],
                           check=True, capture_output=True, timeout=60)
            with open(dst, "rb") as f:
                return f.read()
    import io
    import pillow_heif                 # optional - ImportError when absent
    from PIL import Image
    pillow_heif.register_heif_opener()
    im = Image.open(io.BytesIO(body))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def api_annotations(base_id, table_id, record_id, text_field="", image_field=""):
    """The saved drawing recipes for one record (may be empty), plus - if the
    editor shows a comments panel - the current text of that field. Opening a
    record is also the moment the printout field heals itself."""
    table = find_table(base_id, table_id)
    af = app_fields(table)
    text_f = field_by_ref(table, text_field)
    img_f = field_by_ref(table, image_field)
    record = get_record(base_id, table_id, record_id)
    ann_map = read_annotations_map(record, af["json"]["id"] if af["json"] else "")
    if img_f:
        try:
            healed = sync_print_projection(base_id, table_id, record_id,
                                           img_f["id"], record=record)
            if healed is not None:
                ann_map = healed
        except Exception:
            pass   # healing is best-effort; opening must never fail because of it
    out = {"annotations": ann_map}
    if text_field:
        value = record.get("fields", {}).get(text_f["id"]) if text_f else None
        out["text"] = value if isinstance(value, str) else ("" if value is None else str(value))
    return out


def parse_data_url(data_url):
    """Split a browser "data:image/...;base64,..." text into type + payload."""
    m = re.match(r"data:(image/[\w.+-]+);base64,(.*)$", data_url, re.S)
    if not m:
        raise AirtableError(400, "The picture arrived in an unexpected format.")
    ctype, b64 = m.group(1), m.group(2)
    if len(b64) * 3 / 4 > 5 * 1024 * 1024:
        raise AirtableError(400, "The picture is over Airtable's 5 MB upload limit "
                                 "even after compression.")
    return ctype, b64


def upload_to_field(base_id, record_id, field, ctype, b64, filename, before_ids):
    """Upload one file into an attachment field; give back the new file's id.
    `field` is the field's schema entry (a dict with id and name)."""
    up_url = "%s/%s/%s/%s/uploadAttachment" % (
        CONTENT_API, base_id, record_id, urllib.parse.quote(field["id"]))
    up = airtable(up_url, {"contentType": ctype, "file": b64, "filename": filename})
    uf = up.get("fields") or {}
    # read the TARGET field from the answer (it may be keyed by id or name);
    # only fall back to scanning if neither key is present
    flist = None
    for key in (field["id"], field["name"]):
        if isinstance(uf.get(key), list):
            flist = uf[key]
            break
    if flist is None:
        for value in uf.values():
            if isinstance(value, list):
                flist = value
    after = [a["id"] for a in flist or [] if isinstance(a, dict) and a.get("id")]
    new_id = next((i for i in after if i not in before_ids), None)
    return new_id, after


def api_upload_asset(body):
    """Store a picture that was pasted INTO an annotation.

    It becomes a small file in the ANNOTATION ASSETS field, and the drawing
    recipe only remembers its id - same trick as with the original images."""
    base_id, table_id, record_id = body["baseId"], body["tableId"], body["recordId"]
    table = find_table(base_id, table_id)
    asset_f = app_fields(table)["asset"]
    if not asset_f:
        raise AirtableError(400, "The field %s is missing on this table - use the yellow "
                                 "banner on the app's first page to add it." % ASSET_FIELD)
    ctype, b64 = parse_data_url(body["imageDataUrl"])
    record = get_record(base_id, table_id, record_id)
    before = [a["id"] for a in record.get("fields", {}).get(asset_f["id"]) or []]
    new_id, _ = upload_to_field(base_id, record_id, asset_f, ctype, b64,
                                body.get("filename") or "pasted picture.png", before)
    if not new_id:
        raise AirtableError(502, "Airtable accepted the picture but did not report it back.")
    return {"attId": new_id}


def collect_asset_ids(node, out):
    """Walk a drawing recipe and note every pasted-picture id it mentions."""
    if isinstance(node, dict):
        if isinstance(node.get("assetAttId"), str):
            out.add(node["assetAttId"])
        for value in node.values():
            collect_asset_ids(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_asset_ids(value, out)


def entry_assets(entry):
    """Which pasted pictures does one index entry reference? New-style entries
    carry the list directly; old-style ones still have the recipe inline."""
    out = set()
    if isinstance(entry, dict):
        if isinstance(entry.get("assets"), list):
            out.update(a for a in entry["assets"] if isinstance(a, str))
        else:
            collect_asset_ids(entry.get("fabric"), out)
    return out


def sync_print_projection(base_id, table_id, record_id, image_field, record=None):
    """THE MIRROR LAW. The printout field (ANNOTATED IMAGES) always equals:
    the print-marked images, in the image folder's order - the annotated
    render where one exists, a plain copy of the original otherwise.

    This runs after every change AND whenever a record is opened, so the
    field heals itself no matter what happened in the meantime: deleted
    images, hand-edits in Airtable, or data from older versions of the app.
    Gives back the (possibly repaired) annotation index."""
    table = find_table(base_id, table_id)
    af = app_fields(table)
    img_f = field_by_ref(table, image_field)
    if not img_f or not af["img"] or not af["json"]:
        return None
    IMG_ID = af["img"]["id"]
    JSON_ID = af["json"]["id"]
    ASSET_ID = af["asset"]["id"] if af["asset"] else ""
    if record is None:
        record = get_record(base_id, table_id, record_id)
    fields = record.get("fields", {})
    annots = read_annotations_map(record, JSON_ID)
    sources = [a for a in fields.get(img_f["id"]) or []
               if isinstance(a, dict) and a.get("id")]

    # SAFETY CATCH. If the record HAS saved annotations but NONE of them
    # belong to the images in this field, the app is almost certainly looking
    # at the WRONG attachment field (for example: the settings were switched
    # to another table and back, and landed on a different field). "Healing"
    # now would throw away every drawing on this record - so touch nothing.
    # The moment the right field is selected again, everything is still there.
    if annots and not any(a["id"] in annots for a in sources):
        return annots
    asset_atts = {a["id"]: a for a in (fields.get(ASSET_ID) if ASSET_ID else None) or []
                  if isinstance(a, dict) and a.get("id")}
    img_atts = {a["id"]: a for a in fields.get(IMG_ID) or []
                if isinstance(a, dict) and a.get("id")}
    changed = False
    removed_assets = set()

    # 1. forget images that are no longer on the record, and their files
    live = {a["id"] for a in sources}
    for src in list(annots):
        if src not in live:
            gone = annots.pop(src)
            changed = True
            removed_assets |= entry_assets(gone)
            if isinstance(gone, dict):
                for key in ("recipeAttId", "flatAttId"):
                    if gone.get(key):
                        removed_assets.add(gone[key])

    # 2. entries written by older versions of this app: mark them as printed
    #    and move their finished render into the assets field (by fresh link)
    legacy_copies = []            # (source id, url, filename)
    for src, e in annots.items():
        if not isinstance(e, dict):
            continue
        if "print" not in e:
            e["print"] = True
            changed = True
        if e.get("annotatedAttId"):
            old_att = img_atts.get(e["annotatedAttId"])
            e["projAttId"] = e.pop("annotatedAttId")
            changed = True
            if old_att and old_att.get("url") and not e.get("flatAttId"):
                legacy_copies.append((src, old_att["url"],
                                      old_att.get("filename") or "annotated.jpg"))

    # 3. what should the printout contain, in which order?
    desired = [a for a in sources
               if isinstance(annots.get(a["id"]), dict) and annots[a["id"]].get("print")]
    img_payload, fresh_copies = [], False
    for a in desired:
        e = annots[a["id"]]
        proj = e.get("projAttId")
        if proj and proj in img_atts:
            img_payload.append({"id": proj})       # the right copy already exists
        else:
            flat = asset_atts.get(e.get("flatAttId") or "")
            pick = flat if (flat and flat.get("url")) else a
            if not pick.get("url"):
                continue                            # nothing usable - skip quietly
            img_payload.append({"url": pick["url"],
                                "filename": pick.get("filename") or "image"})
            fresh_copies = True

    asset_payload = None
    if ASSET_ID and (removed_assets or legacy_copies):
        asset_payload = [{"id": i} for i in asset_atts if i not in removed_assets]
        asset_payload += [{"url": u, "filename": fn} for _, u, fn in legacy_copies]

    current_ids = [a["id"] for a in fields.get(IMG_ID) or []
                   if isinstance(a, dict) and a.get("id")]
    if not (fresh_copies or changed or asset_payload is not None
            or [d.get("id") for d in img_payload] != current_ids):
        return annots                               # everything already true

    patch = {IMG_ID: img_payload, JSON_ID: json.dumps(annots)}
    if asset_payload is not None:
        patch[ASSET_ID] = asset_payload
    resp = patch_record(base_id, table_id, record_id, patch)
    rf = resp.get("fields", {})

    # remember which copy in the printout belongs to which image
    fix = False
    new_img = [a for a in rf.get(IMG_ID) or [] if isinstance(a, dict) and a.get("id")]
    if len(new_img) == len(img_payload) == len(desired):
        for a, att in zip(desired, new_img):
            if annots[a["id"]].get("projAttId") != att["id"]:
                annots[a["id"]]["projAttId"] = att["id"]
                fix = True
    if legacy_copies:
        new_assets = [a for a in rf.get(ASSET_ID) or []
                      if isinstance(a, dict) and a.get("id")]
        tail = new_assets[len(new_assets) - len(legacy_copies):]
        for (src, _u, _fn), att in zip(legacy_copies, tail):
            if src in annots and annots[src].get("flatAttId") != att["id"]:
                annots[src]["flatAttId"] = att["id"]
                fix = True
    if fix:
        patch_record(base_id, table_id, record_id, {JSON_ID: json.dumps(annots)})
    return annots


def api_reorder_images(body):
    """Change the order of the images on a record - Airtable keeps whatever
    order we send back. The printout field follows via the mirror law."""
    base_id, table_id, record_id = body["baseId"], body["tableId"], body["recordId"]
    table = find_table(base_id, table_id)
    img_f = field_by_ref(table, body["imageField"])
    if not img_f:
        raise AirtableError(400, "The image field doesn't exist in this table any more - "
                                 "open Settings and pick it again.")
    order = body.get("order") or []
    record = get_record(base_id, table_id, record_id)
    current = record.get("fields", {}).get(img_f["id"]) or []
    by_id = {a["id"]: a for a in current if isinstance(a, dict) and a.get("id")}
    new_list = [{"id": i} for i in order if i in by_id]
    new_list += [{"id": a["id"]} for a in current
                 if a.get("id") and a["id"] not in set(order)]
    resp = patch_record(base_id, table_id, record_id, {img_f["id"]: new_list})
    sync_print_projection(base_id, table_id, record_id, img_f["id"], record=resp)
    return {"ok": True}


def api_set_print(body):
    """Put one image into the printout, or take it out. Non-destructive:
    drawings and renders stay stored either way."""
    base_id, table_id, record_id = body["baseId"], body["tableId"], body["recordId"]
    att_id = body["attId"]
    table = find_table(base_id, table_id)
    af = app_fields(table)
    img_f = field_by_ref(table, body["imageField"])
    if not af["json"] or not af["img"]:
        raise AirtableError(400, "The app's helper fields are missing on this table - "
                                 "add them under Settings first.")
    if not img_f:
        raise AirtableError(400, "The image field doesn't exist in this table any more - "
                                 "open Settings and pick it again.")
    record = get_record(base_id, table_id, record_id)
    annots = read_annotations_map(record, af["json"]["id"])
    entry = annots.get(att_id)
    if not isinstance(entry, dict):
        src = next((a for a in record.get("fields", {}).get(img_f["id"]) or []
                    if isinstance(a, dict) and a.get("id") == att_id), None)
        if not src:
            raise AirtableError(404, "That image is no longer on the record.")
        entry = {"filename": src.get("filename") or "image"}
        annots[att_id] = entry
    entry["print"] = bool(body.get("print"))
    resp = patch_record(base_id, table_id, record_id,
                        {af["json"]["id"]: json.dumps(annots)})
    sync_print_projection(base_id, table_id, record_id, img_f["id"], record=resp)
    return {"ok": True, "print": entry["print"]}


def api_add_photo(body):
    """Add a new photo to the record's own image field, straight from the app."""
    base_id, table_id, record_id = body["baseId"], body["tableId"], body["recordId"]
    table = find_table(base_id, table_id)
    img_f = field_by_ref(table, body["imageField"])
    if not img_f:
        raise AirtableError(400, "The image field doesn't exist in this table any more - "
                                 "open Settings and pick it again.")
    ctype, b64 = parse_data_url(body["imageDataUrl"])
    record = get_record(base_id, table_id, record_id)
    before = [a["id"] for a in record.get("fields", {}).get(img_f["id"]) or []
              if isinstance(a, dict) and a.get("id")]
    new_id, _ = upload_to_field(base_id, record_id, img_f, ctype, b64,
                                body.get("filename") or "photo.jpg", before)
    if not new_id:
        raise AirtableError(502, "Airtable accepted the photo but did not report it back.")
    return {"attId": new_id}


_rename_probe = {}   # (baseId, tableId, imageField) -> bool

def api_can_rename(body):
    """Can images in this field be renamed at all?

    Synced tables and computed fields (lookups etc.) reject every write.
    The test writes the field back UNCHANGED - harmless on a normal field,
    an error on a read-only one - and the answer is remembered per table."""
    table = find_table(body["baseId"], body["tableId"])
    img_f = field_by_ref(table, body["imageField"])
    if not img_f:
        return {"canRename": False}
    key = (body["baseId"], body["tableId"], img_f["id"])
    if key in _rename_probe:
        return {"canRename": _rename_probe[key]}
    record = get_record(body["baseId"], body["tableId"], body["recordId"])
    atts = [a for a in record.get("fields", {}).get(img_f["id"]) or []
            if isinstance(a, dict) and a.get("id")]
    if not atts:
        return {"canRename": False}    # nothing to rename (and never write [])
    ok = True
    try:
        patch_record(body["baseId"], body["tableId"], body["recordId"],
                     {img_f["id"]: [{"id": a["id"]} for a in atts]})
    except AirtableError:
        ok = False
    _rename_probe[key] = ok
    return {"canRename": ok}


def api_rename_image(body):
    """Rename one image, in place.

    Airtable cannot rename an attachment, so the file is re-attached from its
    own address under the new name - which gives it a NEW id. Everything that
    hangs off the old id follows along: the drawing entry in ANNOTATIONS DATA
    moves to the new id, the finished render and the recipe file in
    ANNOTATION ASSETS are re-attached under matching names, and the printout
    field is rebuilt by the mirror law."""
    base_id, table_id, record_id = body["baseId"], body["tableId"], body["recordId"]
    att_id = body["attId"]
    table = find_table(base_id, table_id)
    af = app_fields(table)
    img_f = field_by_ref(table, body["imageField"])
    if not img_f:
        raise AirtableError(400, "The image field doesn't exist in this table any more - "
                                 "open Settings and pick it again.")
    stem = re.sub(r'[\\/:*?"<>|]+', '', (body.get("newStem") or "")).strip()
    if not stem:
        raise AirtableError(400, "The name cannot be empty.")
    record = get_record(base_id, table_id, record_id)
    fields = record.get("fields", {})
    atts = [a for a in fields.get(img_f["id"]) or []
            if isinstance(a, dict) and a.get("id")]
    att = next((a for a in atts if a["id"] == att_id), None)
    if not att or not att.get("url"):
        raise AirtableError(404, "That image is no longer on the record.")

    m = re.search(r"(\.[A-Za-z0-9]+)$", att.get("filename") or "")
    ext = m.group(1) if m else ""
    taken = {a.get("filename") for a in atts if a["id"] != att_id}
    name, n = stem + ext, 2
    while name in taken:                       # duplicates get (2), (3), ...
        name = "%s (%d)%s" % (stem, n, ext)
        n += 1

    # 1. swap the attachment for a same-bytes copy that carries the new name
    payload = [{"id": a["id"]} if a["id"] != att_id
               else {"url": att["url"], "filename": name} for a in atts]
    resp = patch_record(base_id, table_id, record_id, {img_f["id"]: payload})
    new_atts = [a for a in resp.get("fields", {}).get(img_f["id"]) or []
                if isinstance(a, dict) and a.get("id")]
    old_ids = {a["id"] for a in atts}
    new_id = next((a["id"] for a in new_atts if a["id"] not in old_ids), None)
    if not new_id:
        raise AirtableError(502, "Airtable accepted the rename but did not report it back.")

    # 2. the drawing entry follows the new id; its companion files are renamed too
    json_id = af["json"]["id"] if af["json"] else ""
    asset_id = af["asset"]["id"] if af["asset"] else ""
    annots = read_annotations_map(record, json_id)
    entry = annots.pop(att_id, None)
    if isinstance(entry, dict):
        entry["filename"] = name
        entry.pop("projAttId", None)           # the printout copy is rebuilt below
        asset_list = [a for a in (fields.get(asset_id) if asset_id else None) or []
                      if isinstance(a, dict) and a.get("id")]
        renames = {}
        for key_name, new_fn in (("flatAttId", stem + " ANNOTATED.jpg"),
                                 ("recipeAttId", stem + " RECIPE.json")):
            aid = entry.get(key_name)
            src_att = next((a for a in asset_list if a["id"] == aid), None)
            if src_att and src_att.get("url"):
                renames[aid] = new_fn
        if renames:
            asset_payload = [{"id": a["id"]} if a["id"] not in renames
                             else {"url": a["url"], "filename": renames[a["id"]]}
                             for a in asset_list]
            aresp = patch_record(base_id, table_id, record_id, {asset_id: asset_payload})
            new_assets = [a for a in aresp.get("fields", {}).get(asset_id) or []
                          if isinstance(a, dict) and a.get("id")]
            if len(new_assets) == len(asset_list):     # positions are preserved
                for pos, old in enumerate(asset_list):
                    if old["id"] in renames:
                        for key_name in ("flatAttId", "recipeAttId"):
                            if entry.get(key_name) == old["id"]:
                                entry[key_name] = new_assets[pos]["id"]
        annots[new_id] = entry
    if not json_id:
        raise AirtableError(400, "The app's ANNOTATIONS DATA field is missing on this "
                                 "table - add it under Settings first.")
    resp2 = patch_record(base_id, table_id, record_id, {json_id: json.dumps(annots)})

    # 3. the printout field mirrors the new state (and new filenames)
    sync_print_projection(base_id, table_id, record_id, img_f["id"], record=resp2)
    return {"attId": new_id, "filename": name}


def api_save(body):
    """Save one annotated image. This is the heart of the app.

    Steps, in order:
      1. Remember which files are in ANNOTATED IMAGES right now.
      2. Upload the new finished picture (Airtable adds it to the field).
      3. Write the drawing recipe into ANNOTATIONS DATA and, in the same
         breath, drop the OLD finished picture for this image so you never
         get duplicates - the field always holds exactly one finished copy
         per original image.
    """
    base_id = body["baseId"]
    table_id = body["tableId"]
    record_id = body["recordId"]
    att_id = body["attId"]                     # id of the ORIGINAL image
    filename = body.get("filename") or "image"
    fabric_json = body["fabric"]               # the drawing recipe
    data_url = body["imageDataUrl"]            # the finished picture

    table = find_table(base_id, table_id)
    af = app_fields(table)
    missing = [name for key, name in (("json", JSON_FIELD), ("img", IMG_FIELD),
                                      ("asset", ASSET_FIELD)) if not af[key]]
    if missing:
        raise AirtableError(400, "This table is missing the app's helper field(s): %s - "
                                 "add them under Settings first." % ", ".join(missing))
    JSON_ID = af["json"]["id"]
    ASSET_F = af["asset"]
    # The comments field arrives as an id (or an old name) - a deleted field
    # must stop the save BEFORE anything is uploaded.
    instr_f = None
    if body.get("instructionsField"):
        instr_f = field_by_ref(table, body["instructionsField"])
        if not instr_f:
            raise AirtableError(400, "The comments field for this table doesn't exist "
                                     "any more - it was probably deleted in Airtable. "
                                     "Open Settings, pick the comments field again, and "
                                     "press Save once more - nothing is lost.")
    img_f = field_by_ref(table, body.get("imageField") or "")

    ctype, b64 = parse_data_url(data_url)

    # 1. What is on the record right now?
    record = get_record(base_id, table_id, record_id)
    annots = read_annotations_map(record, JSON_ID)
    old_entry = annots.get(att_id)
    if not isinstance(old_entry, dict):
        old_entry = {}
    old_used = set()                             # pasted pictures referenced so far
    for e in annots.values():
        old_used |= entry_assets(e)

    # A tidy name for the finished file: "photo.jpg" -> "photo ANNOTATED.jpg"
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename) or "image"
    out_name = stem + " ANNOTATED.jpg"

    # 2. Upload the finished render AND the drawing recipe - both live in the
    #    hidden assets field. The printout field is rebuilt from them below,
    #    which is what makes taking images in and out of the printout cheap.
    asset_before = [a["id"] for a in record.get("fields", {}).get(ASSET_F["id"]) or []
                    if isinstance(a, dict) and a.get("id")]
    flat_id, after1 = upload_to_field(base_id, record_id, ASSET_F, ctype, b64,
                                      out_name, asset_before)
    this_assets = set()
    collect_asset_ids(fabric_json, this_assets)
    recipe_b64 = base64.b64encode(json.dumps(fabric_json).encode("utf-8")).decode()
    recipe_id, after2 = upload_to_field(base_id, record_id, ASSET_F,
                                        "application/json", recipe_b64,
                                        stem + " RECIPE.json", after1 or asset_before)
    if not flat_id or not recipe_id:
        raise AirtableError(502, "Airtable accepted the upload but did not report it "
                                 "back - please try saving again.")

    # 3. Update the index. Annotating puts the image into the printout,
    #    unless the user had deliberately taken it out.
    annots[att_id] = {
        "filename": filename,
        "print": old_entry.get("print", True),
        "recipeAttId": recipe_id,
        "flatAttId": flat_id,
        "assets": sorted(this_assets),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    recipe_text = json.dumps(annots)
    if len(recipe_text) > 95000:
        raise AirtableError(400, "The annotation index for this record has grown too "
                                 "large - this should not happen; please report it.")

    # 4. Tidy the assets field: drop the old render and recipe of this image,
    #    and pasted pictures that are no longer part of any drawing. (Pasted
    #    pictures never saved into a drawing yet are left alone - they may
    #    belong to an edit still open in another tab.)
    new_used = set()
    for e in annots.values():
        new_used |= entry_assets(e)
    removed = old_used - new_used
    for key in ("recipeAttId", "flatAttId"):
        if old_entry.get(key):
            removed.add(old_entry[key])
    keep_assets = [{"id": i} for i in (after2 or []) if i not in removed]

    fields_patch = {JSON_ID: recipe_text, ASSET_F["id"]: keep_assets}
    # The comments written next to the drawing, if that panel is in use.
    if instr_f:
        fields_patch[instr_f["id"]] = body.get("instructionsText") or ""
    resp = patch_record(base_id, table_id, record_id, fields_patch)

    # 5. Rebuild the printout field so it mirrors the folder again.
    sync_print_projection(base_id, table_id, record_id,
                          img_f["id"] if img_f else "", record=resp)
    return {"ok": True, "print": annots[att_id]["print"]}


# =============================================================================
#  THE LOCAL WEB SERVER - receives clicks from the browser page below
# =============================================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Keep the terminal quiet - only real problems get printed.
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _fail(self, e):
        if isinstance(e, AirtableError):
            self._send(e.status if 400 <= e.status <= 599 else 502, {"error": str(e)})
        else:
            self._send(500, {"error": "Unexpected problem: %s" % e})

    def _guard(self):
        """Only this app's OWN page may talk to the app.

        Web pages from the internet can make a browser send requests to
        127.0.0.1 behind the user's back; their Host / Origin headers give
        them away, so anything that is not local is turned down here.
        The PORT is checked too: another program running on this computer
        (a dev server, some other local tool) is just as much a stranger
        as the internet is."""
        my_port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip()
        hostname = host.split(":")[0].strip("[]").lower()
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urllib.parse.urlparse(origin)
            ohost = (o.hostname or "").lower()
            oport = o.port or (443 if o.scheme == "https" else 80)
            if ohost not in ("127.0.0.1", "localhost", "::1") or oport != my_port:
                return False
        return True

    def do_GET(self):
        if not self._guard():
            self._send(403, {"error": "Refused: the request did not come from this app's own page."})
            return
        parsed = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/":
                self._send(200, load_page().encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/fabric.js":
                # the drawing library, from the verified local copy
                self._send(200, fabric_js_bytes(),
                           "application/javascript; charset=utf-8",
                           cache="public, max-age=86400")
            elif parsed.path == "/api/boot":
                tok = SETTINGS.get("token") or ""
                self._send(200, {
                    "engine": ENGINE_VERSION,
                    "hasToken": bool(tok),
                    # enough of the token to recognise it, never the whole thing
                    "tokenHint": (tok[:3] + "••••••••••" + tok[-4:]) if len(tok) > 10 else "",
                    "remembered": {k: SETTINGS.get(k) for k in
                                   ("baseId", "tableId", "viewId", "imageField",
                                    "nameField", "instructionsField",
                                    "instructionsOn", "layoutMode", "customColors",
                                    "tableChoices", "density", "recentRecords")},
                    "jsonField": JSON_FIELD, "imgField": IMG_FIELD,
                    "assetField": ASSET_FIELD,
                    "port": self.server.server_address[1],
                })
            elif parsed.path == "/api/bases":
                self._send(200, api_bases())
            elif parsed.path == "/api/tables":
                self._send(200, api_tables(q["baseId"]))
            elif parsed.path == "/api/records":
                # If a dropdown was still empty (say, Airtable was slow and the
                # button was pressed early), explain instead of tripping over it.
                missing = [k for k in ("baseId", "tableId", "imageField", "nameField")
                           if not q.get(k)]
                if missing:
                    raise AirtableError(400, "The table list had not finished loading yet - "
                                             "wait a moment and press \"Show images\" again.")
                self._send(200, api_records(q["baseId"], q["tableId"], q.get("viewId", ""),
                                            q["imageField"], q["nameField"],
                                            q.get("notesField", ""),
                                            q.get("offset", ""),
                                            q.get("filterField", ""),
                                            q.get("modifiedField", "")))
            elif parsed.path == "/api/record":
                self._send(200, api_single_record(q["baseId"], q["tableId"], q["recordId"],
                                                  q["imageField"], q["nameField"],
                                                  q.get("notesField", ""),
                                                  q.get("filterField", ""),
                                                  q.get("modifiedField", "")))
            elif parsed.path == "/api/annotations":
                self._send(200, api_annotations(q["baseId"], q["tableId"], q["recordId"],
                                                q.get("textField", ""),
                                                q.get("imageField", "")))
            elif parsed.path == "/api/image":
                body, ctype = api_image_bytes(q["baseId"], q["tableId"], q["recordId"],
                                              q["imageField"], q["attId"])
                # let the browser keep it for a few minutes - reopening an
                # image then skips the whole download
                self._send(200, body, ctype, cache="private, max-age=300")
            else:
                self._send(404, {"error": "Unknown address."})
        except Exception as e:
            self._fail(e)

    def do_POST(self):
        if not self._guard():
            self._send(403, {"error": "Refused: the request did not come from this app's own page."})
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/token":
                SETTINGS["token"] = body.get("token", "").strip()
                save_settings(SETTINGS)
                self._send(200, {"ok": True})
            elif parsed.path == "/api/remember":
                # Remember the dropdown choices for next time.
                for k in ("baseId", "tableId", "viewId", "imageField",
                          "nameField", "instructionsField",
                          "instructionsOn", "layoutMode", "customColors",
                          "tableChoices", "density", "recentRecords"):
                    if k in body:
                        SETTINGS[k] = body[k]
                save_settings(SETTINGS)
                self._send(200, {"ok": True})
            elif parsed.path == "/api/ensure_fields":
                self._send(200, api_ensure_fields(body["baseId"], body["tableId"],
                                                  body.get("field", "")))
            elif parsed.path == "/api/check_write":
                self._send(200, api_check_write(body["baseId"], body["tableId"], body["recordId"]))
            elif parsed.path == "/api/can_rename":
                self._send(200, api_can_rename(body))
            elif parsed.path == "/api/rename_image":
                self._send(200, api_rename_image(body))
            elif parsed.path == "/api/reorder_images":
                self._send(200, api_reorder_images(body))
            elif parsed.path == "/api/set_print":
                self._send(200, api_set_print(body))
            elif parsed.path == "/api/add_photo":
                self._send(200, api_add_photo(body))
            elif parsed.path == "/api/upload_asset":
                self._send(200, api_upload_asset(body))
            elif parsed.path == "/api/save":
                self._send(200, api_save(body))
            else:
                self._send(404, {"error": "Unknown address."})
        except Exception as e:
            self._fail(e)


# =============================================================================
#  THE BROWSER PAGE - everything you see and click lives in annotator.html,
#  which sits right next to this script. It is re-read on every page load,
#  so editing the HTML never needs a server restart.
# =============================================================================

PAGE_FILE = os.path.join(APP_DIR, "annotator.html")

def load_page():
    try:
        with open(PAGE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ("<!DOCTYPE html><html><body style=\"font-family:sans-serif;padding:40px\">"
                "<h2>annotator.html is missing</h2>"
                "<p>The app's page lives in a file called <b>annotator.html</b> that must "
                "sit in the same folder as airtable_image_annotator_app.py. Put it back "
                "(or restore the folder from a backup) and reload this page.</p>"
                "</body></html>")


# --- the drawing library, kept locally and verified ---------------------------
# Fabric.js used to load from a public CDN, which meant no internet = no app,
# and a hacked CDN could have slipped in bad code. Now a copy lives next to
# this script and is checked against a known fingerprint before every use.
FABRIC_FILE = os.path.join(APP_DIR, "fabric.min.js")
FABRIC_URL = "https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.0/fabric.min.js"
FABRIC_SHA256 = "f3a3763020189d69b8d2b64197172682b6d90f8f90fcac52d799b0cf64a9870a"

def fabric_js_bytes():
    """The drawing library's bytes - from disk when the verified copy is
    there, downloaded (and verified, and kept) when it is not."""
    import hashlib
    try:
        with open(FABRIC_FILE, "rb") as f:
            body = f.read()
        if hashlib.sha256(body).hexdigest() == FABRIC_SHA256:
            return body
    except Exception:
        pass
    with urllib.request.urlopen(FABRIC_URL, timeout=60) as r:
        body = r.read()
    if hashlib.sha256(body).hexdigest() != FABRIC_SHA256:
        raise AirtableError(502, "The drawing library arrived damaged (its fingerprint "
                                 "does not match) - please reload to try again.")
    try:
        with open(FABRIC_FILE, "wb") as f:
            f.write(body)
    except Exception:
        pass   # cannot cache it - serve it anyway
    return body


# =============================================================================
#  START EVERYTHING
# =============================================================================

def already_running_at(port):
    """Is another copy of THIS app already answering on that port?"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/boot" % port, timeout=2) as r:
            return "jsonField" in json.load(r)
    except Exception:
        return False


def selftest():
    """A quick health check, no server and no Airtable needed:
         python3 airtable_image_annotator_app.py --selftest
    Run it after editing any of the app's files - it catches the classic
    'one edit broke the page' mistakes before they reach the browser."""
    import hashlib
    problems = []
    page = load_page()
    for needle in ("<!DOCTYPE html>", "Airtable Image Annotator",
                   'src="/fabric.js"', "id=\"btnDownload\"", "function saveNow"):
        if needle not in page:
            problems.append("annotator.html looks wrong - missing %r" % needle)
    try:
        ctype, _ = parse_data_url("data:image/png;base64,AAAA")
        assert ctype == "image/png"
    except Exception as e:
        problems.append("parse_data_url broke: %s" % e)
    try:
        with open(FABRIC_FILE, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != FABRIC_SHA256:
                problems.append("fabric.min.js does not match its fingerprint "
                                "(a fresh copy will be downloaded on demand)")
    except Exception:
        problems.append("fabric.min.js is missing "
                        "(it will be downloaded on first use - needs internet once)")
    if problems:
        for p in problems:
            print("PROBLEM:", p)
        sys.exit(1)
    print("Self-test OK - the page, the drawing library and the helpers all look right.")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    # Find a free door number (port) on this computer for the app to use.
    # If a copy of the app is already running, don't start a second one -
    # just open its page and step aside. (The links in Airtable all point at
    # one fixed door number, so a stray second copy used to break them.)
    server = None
    port = 8765
    for candidate in range(8765, 8790):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            if already_running_at(candidate):
                url = "http://127.0.0.1:%d" % candidate
                print("The app is already running - opening its page at %s" % url)
                print("This window may be closed; the other one is the app.")
                webbrowser.open(url)
                return
            continue
    if server is None:
        print("Could not find a free port between 8765 and 8789.")
        sys.exit(1)

    url = "http://127.0.0.1:%d" % port
    print("Airtable Image Annotator is running.")
    print("If the browser did not open by itself, go to:  %s" % url)
    print("Keep this window open while you work. Press Ctrl+C to quit.")
    if port != 8765:
        print("")
        print("NOTE: the app's usual door number 8765 is taken by some other")
        print("program, so it is using %d instead. The pencil buttons/links" % port)
        print("in Airtable point at 8765 and will NOT reach this window until")
        print("that other program is closed and the annotator is restarted.")
    if "--no-browser" not in sys.argv:      # for tests and scripted starts
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
