# Airtable Image Annotator

Draw on the images that live in your Airtable records — crop, arrows (straight or
curved), boxes, circles, freehand pen, highlighter, text, numbered markers,
pasted-in pictures, blur patches, and a spotlight — **without ever touching the
original photo**.

Two things make it special:

1. **Annotations stay editable forever.** Open the same image months later and
   every arrow, box and text is still movable and deletable.
2. **Print tools get a finished picture automatically.** Every save also produces
   a flattened copy (drawings baked in) that Documint, Page Designer, or any
   other print tool can use directly.

The app is a tiny web server that runs **only on your own computer** — nothing is
installed system-wide, nothing runs in the cloud, and the only outside service it
ever talks to is your own Airtable base.

## How to run it

1. **Download this project.** Click the green **Code** button above → **Download
   ZIP**, then unzip it. (Keep all the files together in one folder.)
2. **Double-click the launcher:**
   - **Mac:** `Annotate Airtable Images.command`
   - **Windows:** `Annotate Airtable Images (Windows).bat`
3. A terminal window opens (that window *is* the app), and the editor appears in
   your web browser. The launcher will offer to install Python for you if it's
   missing.
4. The first time, open **Settings** and paste in your Airtable **personal access
   token**, then pick your base, table, and image field.

Full details are in **`HOW TO RUN THE APP.txt`** and **`ABOUT THIS APP.md`**.

## What you need

- **Python 3** (free — the launcher can install it for you).
- An **Airtable personal access token** with access to your base. Create one at
  <https://airtable.com/create/tokens> with the scopes `data.records:read`,
  `data.records:write`, and `schema.bases:write`.

## Your data & privacy

Everything is stored in **your own Airtable base**, in three fields the app can
create for you (`ANNOTATIONS DATA`, `ANNOTATION ASSETS`, `ANNOTATED IMAGES`).
There is no separate database and no account — delete those three fields and every
trace of the app is gone.

Your Airtable token is stored only in `annotator_settings.json` on your own
computer. **That file is never shared** (it's excluded from this project).
