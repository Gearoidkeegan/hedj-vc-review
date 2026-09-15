#!/usr/bin/env python3
"""Prepare an anonymous, opt-in result from a finished review, for a feedback form.

Usage:
    python share_result.py <review_dir> [--pilot LABEL] [--paying LABEL] [--arr LABEL]
                           [--funding LABEL] [--round LABEL] [--sector LABEL] [--open]
    python share_result.py --configure "<pre-filled form link>"

Nothing is sent. The script reduces a finished review to coarse, anonymous
answers, prints exactly what they are, saves them as anonymous-result.json in
the review folder, and builds a pre-filled link to the project's feedback form.
The reviewer opens the link, corrects anything they know better, answers the
remaining questions and submits the form themselves. --open also opens the link
in the default browser.

Included: plugin version, month, stage, depth, model mode, investability and
documents-corrected scores rounded to the nearest 5, recommendation, AI-usage
score, whether hidden text was found, and the labels passed for pilot status,
paying customers, ARR band, funding to date, round size and sector. Never
included: the company, people, places, file names, exact scores or dates, or
any text from the materials.

--configure is for the maintainer. In the form service, build a pre-filled link
with each pre-filled question answered by its field key (version, month, stage,
depth, models, score, corrected, recommendation, ai_usage, hidden_text, pilot,
paying, arr, funding, round, sector), then pass the link here. The script
records which URL parameter carries which answer in sharing/share-config.json.
--config PATH uses another settings file (for testing).

Standard library only. No network access: urllib.parse only builds the link.
"""

import json
import os
import sys
import urllib.parse
import webbrowser
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
PLUGIN_JSON = os.path.normpath(os.path.join(SKILL_DIR, "..", "..", ".claude-plugin", "plugin.json"))
CONFIG = os.path.join(SKILL_DIR, "sharing", "share-config.json")

sys.path.insert(0, HERE)
import build_deliverables as bd  # noqa: E402  same folder; the shared numbers match the slide

FIELDS = ["version", "month", "stage", "depth", "models", "score", "corrected",
          "recommendation", "ai_usage", "hidden_text", "pilot", "paying", "arr", "funding",
          "round", "sector"]
TITLES = {"version": "Plugin version", "month": "Month", "stage": "Stage", "depth": "Depth",
          "models": "Model mode", "score": "Investability score (nearest 5)",
          "corrected": "Documents-corrected score (nearest 5)", "recommendation": "Recommendation",
          "ai_usage": "AI-usage score", "hidden_text": "Hidden text found",
          "pilot": "Pilot completed", "paying": "Paying customers", "arr": "ARR band",
          "funding": "Funding raised to date", "round": "Round size", "sector": "Sector"}
CHOICES = {
    "pilot": ["yes", "no", "not stated"],
    "paying": ["yes", "no", "not stated"],
    "arr": ["pre-revenue", "under EUR 100k", "EUR 100k-500k", "EUR 500k-1m", "EUR 1m-5m",
            "over EUR 5m", "not stated"],
    "funding": ["none", "under EUR 500k", "EUR 500k-2m", "EUR 2m-10m", "over EUR 10m",
                "not stated"],
    "round": ["under EUR 500k", "EUR 500k-1m", "EUR 1m-3m", "EUR 3m-10m", "over EUR 10m",
              "not stated"],
    "sector": ["b2b software", "fintech", "health", "climate and energy",
               "deep tech and hardware", "consumer", "marketplace", "other"],
}
DEFAULTS = {"pilot": "not stated", "paying": "not stated", "arr": "not stated",
            "funding": "not stated", "round": "not stated", "sector": "other"}
ABOUT = ("Written by share_result.py --configure. With no form_url, sharing stays switched "
         "off and the script only prints the anonymous result.")


def squash(text):
    return " ".join(str(text).lower().replace("-", " ").replace("_", " ").split())


def canonical(field, value):
    return next((label for label in CHOICES[field] if squash(label) == squash(value)), None)


def nearest_five(value):
    return str(int(5 * round(value / 5)))


def plugin_version():
    try:
        with open(PLUGIN_JSON, encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


def build_result(review_dir, answers):
    """The anonymous answers for a finished review, or None if it isn't finished."""
    d = bd.extract(review_dir)
    if d["recommendation"] == "NOT STATED":
        return None
    prof = bd.as_dict(bd.load(review_dir, "deal-profile.json", {}))
    stage = str(prof.get("stage", "")).lower()
    models = str(prof.get("models", "")).lower()
    hidden = d["hidden_value"]
    return {
        "version": plugin_version(),
        "month": date.today().strftime("%Y-%m"),
        "stage": stage if stage in bd.STAGE_WEIGHTS else "not stated",
        "depth": d["depth"] if d["depth"] in ("full", "quick") else "not stated",
        "models": models if models in ("judicious", "max") else "not stated",
        "score": nearest_five(d["score100"]),
        "corrected": ("not computed" if d["corrected_text"] == "not computed"
                      else nearest_five(d["corrected100"])),
        "recommendation": d["recommendation"].lower(),
        "ai_usage": "not run" if d["ai_score"] is None else f"{d['ai_score']:g}",
        "hidden_text": ("not checked" if hidden == "NOT CHECKED"
                        else "no" if hidden == "NONE FOUND" else "yes"),
        **answers,
    }


def load_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def form_link(result, config):
    base = config.get("form_url") or ""
    fields = config.get("fields") or {}
    if not base.startswith("https://") or any(f not in fields for f in FIELDS):
        return None
    query = urllib.parse.urlencode([(fields[f], result[f]) for f in FIELDS])
    return base + ("&" if "?" in base else "?") + query


def configure(link, path):
    parts = urllib.parse.urlsplit(link.strip())
    if parts.scheme != "https" or not parts.netloc:
        print("error: that isn't a form link (it must start with https://)")
        return 2
    fields, keep = {}, []
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        key = value.strip().lower()
        if key in FIELDS and key not in fields:
            fields[key] = name
        else:
            keep.append((name, value))
    missing = [f for f in FIELDS if f not in fields]
    if missing:
        print("error: the link doesn't carry an answer for: " + ", ".join(missing)
              + ". In the form's pre-filled link, answer each pre-filled question with its "
                "field key, then copy the link again.")
        return 2
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(keep), ""))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"_about": ABOUT, "form_url": base,
                   "fields": {f: fields[f] for f in FIELDS}}, fh, indent=2)
        fh.write("\n")
    print(f"configured {len(FIELDS)} fields for {parts.netloc} in {path}")
    return 0


def parse(argv):
    opts, args, i = {}, [], 0
    valued = {"configure", "config", *CHOICES}
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            name = token[2:]
            if name == "open":
                opts["open"] = True
                i += 1
                continue
            if name not in valued:
                raise ValueError(f"unknown option {token}")
            if i + 1 >= len(argv):
                raise ValueError(f"{token} needs a value")
            opts[name] = argv[i + 1]
            i += 2
            continue
        args.append(token)
        i += 1
    return opts, args


def main():
    if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        opts, args = parse(sys.argv[1:])
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    config_path = opts.get("config", CONFIG)
    if "configure" in opts:
        return configure(opts["configure"], config_path)
    if len(args) != 1 or not os.path.isdir(args[0]):
        print(__doc__)
        return 2
    review_dir = args[0]

    answers = {}
    for field in CHOICES:
        label = canonical(field, opts.get(field, DEFAULTS[field]))
        if label is None:
            print(f"error: --{field} must be one of: " + "; ".join(CHOICES[field]))
            return 2
        answers[field] = label

    result = build_result(review_dir, answers)
    if result is None:
        print("error: this review has no recommendation yet. Finish it before sharing.")
        return 1
    with open(os.path.join(review_dir, "anonymous-result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")

    print("Anonymous result (nothing has been sent)")
    for field in FIELDS:
        print(f"  {TITLES[field]:40} {result[field]}")
    print(f"Saved in {os.path.join(review_dir, 'anonymous-result.json')}")

    link = form_link(result, load_config(config_path))
    if link is None:
        print("\nSharing isn't set up in this copy of VC Review: sharing/share-config.json has "
              "no form link yet. Nothing was sent.")
        return 0
    print("\nOpen this link, correct any answer you know better, answer the last questions and "
          "press Submit. Don't add names or anything that identifies the company:")
    print(link)
    if opts.get("open"):
        webbrowser.open(link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
