import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config — read at request time so missing vars don't crash startup ────────
def cfg(key, default=None):
    val = os.environ.get(key, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ── Google Sheets connection ─────────────────────────────────────────────────
def get_sheets():
    creds_dict = json.loads(cfg("GOOGLE_SERVICE_ACCOUNT_JSON"))
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(cfg("GOOGLE_SHEET_ID"))


def get_ws(sh, name):
    """Get a worksheet by name, creating it with headers if it doesn't exist."""
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=200, cols=10)
        headers = {
            "members": ["display_name", "user_id", "family"],
            "points":  ["family", "points"],
            "log":     ["timestamp", "display_name", "family", "image_url", "status"],
        }
        if name in headers:
            ws.append_row(headers[name])
        return ws


# ── Members sheet helpers ─────────────────────────────────────────────────────
def find_member_by_id(ws, user_id):
    """Return (row_index, record) by GroupMe user_id, or None."""
    for i, r in enumerate(ws.get_all_records(), start=2):
        if str(r["user_id"]) == str(user_id):
            return i, r
    return None


def find_member_by_name(ws, name):
    """Return (row_index, record) by display_name (case-insensitive), or None."""
    target = name.lower().strip()
    for i, r in enumerate(ws.get_all_records(), start=2):
        if r["display_name"].lower().strip() == target:
            return i, r
    return None


# ── Points sheet helpers ──────────────────────────────────────────────────────
def get_family_row(ws_points, family):
    """Return (row_index, points) for a family, or None."""
    for i, r in enumerate(ws_points.get_all_records(), start=2):
        if r["family"].lower() == family.lower():
            return i, int(r["points"])
    return None


def add_point(ws_points, family):
    result = get_family_row(ws_points, family)
    if result:
        idx, pts = result
        ws_points.update_cell(idx, 2, pts + 1)
    else:
        ws_points.append_row([family, 1])


def remove_point(ws_points, family):
    result = get_family_row(ws_points, family)
    if result:
        idx, pts = result
        ws_points.update_cell(idx, 2, max(0, pts - 1))


# ── Log sheet helpers ─────────────────────────────────────────────────────────
def log_submission(ws_log, name, family, image_url, status="approved"):
    ws_log.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        name, family, image_url, status
    ])


def find_latest_by_status(ws_log, name, status):
    """Find the most recent log row for a name with the given status."""
    records = ws_log.get_all_records()
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        if r["display_name"].lower() == name.lower() and r["status"] == status:
            return i + 2, r  # +2: header row + 0-index offset
    return None


# ── GroupMe messaging ─────────────────────────────────────────────────────────
def send_message(text):
    try:
        requests.post(
            "https://api.groupme.com/v3/bots/post",
            json={"bot_id": cfg("GROUPME_BOT_ID"), "text": text},
            timeout=5,
        )
    except Exception as e:
        logging.error(f"Failed to send message: {e}")


# ── Command handlers ──────────────────────────────────────────────────────────
def cmd_scores(sh):
    ws = get_ws(sh, "points")
    records = ws.get_all_records()
    if not records:
        return "No scores yet! Submit a photo to get on the board."
    ranked = sorted(records, key=lambda r: int(r["points"]), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏅 Family Olympics Standings\n"]
    for i, r in enumerate(ranked):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {r['family']}: {r['points']} pt{'s' if int(r['points']) != 1 else ''}")
    return "\n".join(lines)


def cmd_families(sh):
    ws = get_ws(sh, "members")
    records = ws.get_all_records()
    if not records:
        return "No families assigned yet. Admin: use 'assign [name] [family]'."
    by_family = {}
    for r in records:
        by_family.setdefault(r["family"], []).append(r["display_name"])
    lines = ["👨‍👩‍👧‍👦 Family Roster\n"]
    for fam in sorted(by_family):
        lines.append(f"{fam}:")
        for m in sorted(by_family[fam]):
            lines.append(f"  • {m}")
        lines.append("")
    return "\n".join(lines).strip()


def cmd_assign(sh, args, is_admin):
    """assign [display name] [family]  — family is last token, name is everything before."""
    if not is_admin:
        return "❌ Admin only."
    if len(args) < 2:
        return "Usage: @bot assign [display name] [family]\nExample: @bot assign John Smith TeamAlpha"
    family = args[-1]
    name = " ".join(args[:-1])
    ws = get_ws(sh, "members")
    existing = find_member_by_name(ws, name)
    if existing:
        idx, _ = existing
        ws.update_cell(idx, 3, family)
        return f"✅ Updated {name} → {family}"
    else:
        ws.append_row([name, "", family])
        return (f"✅ Assigned {name} → {family}\n"
                f"Their user ID will be auto-filled when they submit a photo.")


def cmd_unassign(sh, args, is_admin):
    if not is_admin:
        return "❌ Admin only."
    if not args:
        return "Usage: @bot unassign [display name]"
    name = " ".join(args)
    ws = get_ws(sh, "members")
    result = find_member_by_name(ws, name)
    if not result:
        return f"❌ {name} not found on the roster."
    ws.delete_rows(result[0])
    return f"✅ Removed {name} from the roster."


def cmd_dispute(sh, args, sender_name):
    """Anyone can dispute a submission. Flags it for admin review."""
    if not args:
        return "Usage: @bot dispute [display name]\nExample: @bot dispute John Smith"
    name = " ".join(args)
    ws_log = get_ws(sh, "log")
    result = find_latest_by_status(ws_log, name, "approved")
    if not result:
        return f"❌ No approved submission found for {name}."
    idx, record = result
    ws_log.update_cell(idx, 5, "disputed")
    return (f"🚩 {sender_name} disputed {name}'s last submission ({record['timestamp']}).\n"
            f"Admin: '@bot approve {name}' to keep the point or '@bot reject {name}' to remove it.")


def cmd_approve(sh, args, is_admin):
    if not is_admin:
        return "❌ Admin only."
    if not args:
        return "Usage: @bot approve [display name]"
    name = " ".join(args)
    ws_log = get_ws(sh, "log")
    result = find_latest_by_status(ws_log, name, "disputed")
    if not result:
        return f"❌ No disputed submission found for {name}."
    ws_log.update_cell(result[0], 5, "approved")
    return f"✅ {name}'s submission approved. Point stands."


def cmd_reject(sh, args, is_admin):
    if not is_admin:
        return "❌ Admin only."
    if not args:
        return "Usage: @bot reject [display name]"
    name = " ".join(args)
    sh2 = sh  # same connection
    ws_log = get_ws(sh2, "log")
    ws_points = get_ws(sh2, "points")
    result = find_latest_by_status(ws_log, name, "disputed")
    if not result:
        return f"❌ No disputed submission found for {name}."
    idx, record = result
    ws_log.update_cell(idx, 5, "rejected")
    remove_point(ws_points, record["family"])
    return f"❌ {name}'s submission rejected. Point removed from {record['family']}."


def cmd_addpoints(sh, args, is_admin):
    """Admin manual point adjustment: addpoints [family] [+/-N]"""
    if not is_admin:
        return "❌ Admin only."
    if len(args) < 2:
        return "Usage: @bot addpoints [family] [number]\nExample: @bot addpoints TeamAlpha 2"
    try:
        delta = int(args[-1])
    except ValueError:
        return "❌ Last argument must be a number."
    family = " ".join(args[:-1])
    ws_points = get_ws(sh, "points")
    result = get_family_row(ws_points, family)
    if result:
        idx, pts = result
        new_pts = max(0, pts + delta)
        ws_points.update_cell(idx, 2, new_pts)
    else:
        ws_points.append_row([family, max(0, delta)])
    sign = "+" if delta >= 0 else ""
    return f"✅ {family}: {sign}{delta} points applied."


HELP_TEXT = """🏅 Olympics Bot Help

📸 Submit a photo:
  Type @OlympicsBot with an image attached
  (type the name manually — don't use the mention picker)

📊 Commands (anyone):
  @OlympicsBot scores
  @OlympicsBot families
  @OlympicsBot dispute [name]

🔒 Admin only:
  @OlympicsBot assign [name] [family]
  @OlympicsBot unassign [name]
  @OlympicsBot approve [name]
  @OlympicsBot reject [name]
  @OlympicsBot addpoints [family] [N]"""


# ── Debug endpoints ───────────────────────────────────────────────────────────
@app.route("/ping", methods=["GET"])
def ping():
    return "Bot is running.", 200


# ── Webhook entry point ───────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    # Log every incoming payload for debugging
    logging.info(f"WEBHOOK RECEIVED: sender={data.get('name')} "
                 f"type={data.get('sender_type')} "
                 f"text={repr(data.get('text'))} "
                 f"attachments={data.get('attachments')}")

    # Ignore bot messages to prevent infinite loops
    if data.get("sender_type") == "bot":
        return jsonify({}), 200

    text        = (data.get("text") or "").strip()
    sender_name = data.get("name", "Unknown")
    sender_id   = str(data.get("user_id", ""))
    attachments = data.get("attachments", [])

    bot_tag   = f"@{cfg('BOT_NAME', 'OlympicsBot')}".lower()
    has_image = any(a.get("type") == "image" for a in attachments)
    image_url = next((a["url"] for a in attachments if a.get("type") == "image"), "")

    # Only respond when the bot name is in the message
    if bot_tag not in text.lower():
        return jsonify({}), 200

    is_admin  = sender_id == cfg("ADMIN_USER_ID")

    # Parse everything after the bot name as "cmd args"
    lower     = text.lower()
    after_tag = text[lower.index(bot_tag) + len(bot_tag):].strip()
    tokens    = after_tag.split()
    cmd       = tokens[0].lower() if tokens else ""
    args      = tokens[1:]

    COMMANDS = {"scores", "families", "assign", "unassign", "dispute",
                "approve", "reject", "addpoints", "help"}

    # ── Photo submission ──────────────────────────────────────────────────────
    if has_image and cmd not in COMMANDS:
        sh         = get_sheets()
        ws_members = get_ws(sh, "members")
        ws_points  = get_ws(sh, "points")
        ws_log     = get_ws(sh, "log")

        member = find_member_by_id(ws_members, sender_id)
        if not member:
            member = find_member_by_name(ws_members, sender_name)
            if member:
                ws_members.update_cell(member[0], 2, sender_id)

        if not member:
            send_message(
                f"❌ {sender_name}, you're not on the roster yet.\n"
                f"Admin: use '@OlympicsBot assign {sender_name} [FamilyName]'"
            )
            return jsonify({}), 200

        family = member[1]["family"]
        add_point(ws_points, family)
        log_submission(ws_log, sender_name, family, image_url)
        send_message(f"✅ Point recorded for {sender_name}! ({family})")
        return jsonify({}), 200

    # ── Text commands ─────────────────────────────────────────────────────────
    sh = get_sheets()

    if cmd == "scores":
        send_message(cmd_scores(sh))
    elif cmd == "families":
        send_message(cmd_families(sh))
    elif cmd == "assign":
        send_message(cmd_assign(sh, args, is_admin))
    elif cmd == "unassign":
        send_message(cmd_unassign(sh, args, is_admin))
    elif cmd == "dispute":
        send_message(cmd_dispute(sh, args, sender_name))
    elif cmd == "approve":
        send_message(cmd_approve(sh, args, is_admin))
    elif cmd == "reject":
        send_message(cmd_reject(sh, args, is_admin))
    elif cmd == "addpoints":
        send_message(cmd_addpoints(sh, args, is_admin))
    else:
        send_message(HELP_TEXT)

    return jsonify({}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

#