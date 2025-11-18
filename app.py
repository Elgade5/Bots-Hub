import os
from flask import Flask, render_template, request, redirect, session, url_for, flash, jsonify
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from datetime import datetime, timedelta
import markdown as md
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET")

# --- SQLAlchemy configuration for SQLite ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///bots.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SITE_ADMIN_ID = os.getenv("SITE_ADMIN_ID")

COOLDOWN_HOURS = 6
user_votes = {}  # { (user_id, bot_id): last_upvote_datetime }

# --- Bot Model for DB ---
class Bot(db.Model):
    id = db.Column(db.String, primary_key=True)
    name = db.Column(db.String, nullable=False)
    avatar_url = db.Column(db.String)
    banner_url = db.Column(db.String)
    description = db.Column(db.Text)
    short_description = db.Column(db.String)
    prefix = db.Column(db.String, default="!")
    website = db.Column(db.String)
    support_server = db.Column(db.String)
    invite_link = db.Column(db.String)
    tags = db.Column(db.Text)  # CSV: "Music,Moderation"
    owner_id = db.Column(db.String, nullable=False)
    owner_name = db.Column(db.String, nullable=False)
    added_date = db.Column(db.String)
    upvotes = db.Column(db.Integer, default=0)
    server_count = db.Column(db.Integer, default=0)
    certified = db.Column(db.Boolean, default=False)

def is_admin(user):
    if not user:
        return False
    return str(user.get("id")) == SITE_ADMIN_ID

def is_bot_owner(bot, user):
    if not user or not bot:
        return False
    return str(bot.owner_id) == str(user.get("id"))

def can_edit_bot(bot, user):
    return is_admin(user) or is_bot_owner(bot, user)

def can_delete_bot(bot, user):
    return is_admin(user) or is_bot_owner(bot, user)

@app.route("/api/fetch-bot-info/<bot_id>")
def api_fetch_bot_info(bot_id):
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.get(f"https://discord.com/api/v10/users/{bot_id}", headers=headers)

    if r.status_code != 200:
        return jsonify({"error": "Bot not found"}), 404

    data = r.json()
    if not data.get("bot"):
        return jsonify({"error": "This ID belongs to a user, not a bot"}), 400

    avatar_hash = data.get("avatar")
    avatar_url = f"https://cdn.discordapp.com/avatars/{bot_id}/{avatar_hash}.png" if avatar_hash else None

    return jsonify({
        "username": data.get("username"),
        "avatar_url": avatar_url or "https://cdn.discordapp.com/embed/avatars/0.png"
    })

# --- Home ---
@app.route("/")
def index():
    search_query = request.args.get("search", "").lower()
    filter_tag = request.args.get("tag", "")
    sort_by = request.args.get("sort", "newest")

    bots = Bot.query
    if search_query:
        bots = bots.filter(Bot.name.ilike(f"%{search_query}%") | Bot.description.ilike(f"%{search_query}%"))
    if filter_tag:
        bots = bots.filter(Bot.tags.like(f"%{filter_tag}%"))
    bots = bots.all()

    if sort_by == "oldest":
        bots = list(reversed(bots))
    elif sort_by == "popular":
        bots.sort(key=lambda x: x.upvotes or 0, reverse=True)

    user = session.get("user")
    return render_template("index.html", 
        bots=bots, 
        user=user,
        is_admin=is_admin(user),
        search_query=search_query,
        filter_tag=filter_tag,
        sort_by=sort_by
    )

# --- Bot Details ---
@app.route("/bot/<bot_id>")
def bot_detail(bot_id):
    bot = Bot.query.filter_by(id=bot_id).first()
    if not bot:
        flash("Bot not found.", "error")
        return redirect(url_for("index"))

    user = session.get("user")
    can_vote = True
    cooldown_msg = None
    if user:
        key = (str(user["id"]), str(bot_id))
        last_vote = user_votes.get(key)
        if last_vote:
            now = datetime.now()
            if now - last_vote < timedelta(hours=COOLDOWN_HOURS):
                can_vote = False
                cooldown_left = last_vote + timedelta(hours=COOLDOWN_HOURS) - now
                hours, remainder = divmod(cooldown_left.seconds, 3600)
                minutes = remainder // 60
                cooldown_msg = f"{hours}h {minutes}m"
    rendered_description = md.markdown(bot.description if bot else "", extensions=["extra"])
    # tags as list for template
    bot_tags = bot.tags.split(",") if bot.tags else []
    return render_template(
        "bot_detail.html",
        bot=bot,
        user=user,
        is_admin=is_admin(user),
        can_edit=can_edit_bot(bot, user),
        can_delete=can_delete_bot(bot, user),
        can_vote=can_vote,
        cooldown_msg=cooldown_msg,
        rendered_description=rendered_description,
        bot_tags=bot_tags,
    )

# --- Add bot (login required) ---
@app.route("/add-bot", methods=["GET", "POST"])
def add_bot():
    if "user" not in session:
        flash("Please login to add a bot.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        bot_id = request.form.get("bot_id")
        description = request.form.get("description")
        short_description = request.form.get("short_description")
        prefix = request.form.get("prefix", "!")
        website = request.form.get("website", "")
        support_server = request.form.get("support_server", "")
        invite_link = request.form.get("invite_link", "")
        tags = request.form.getlist("tags")
        tags_str = ",".join(tags)

        if Bot.query.filter_by(id=bot_id).first():
            flash("This bot is already listed.", "error")
            return redirect(url_for("add_bot"))

        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
        r = requests.get(f"https://discord.com/api/v10/users/{bot_id}", headers=headers)
        if r.status_code != 200:
            flash("Invalid Bot ID or Discord API error.", "error")
            return redirect(url_for("add_bot"))

        bot_data = r.json()
        if not bot_data.get("bot"):
            flash("This ID is not a bot account.", "error")
            return redirect(url_for("add_bot"))

        bot_name = bot_data.get("username")
        avatar_hash = bot_data.get("avatar")
        avatar_url = f"https://cdn.discordapp.com/avatars/{bot_id}/{avatar_hash}.png" if avatar_hash else None
        banner_hash = bot_data.get("banner")
        banner_url = f"https://cdn.discordapp.com/banners/{bot_id}/{banner_hash}.png" if banner_hash else None

        new_bot = Bot(
            id=bot_id,
            name=bot_name,
            avatar_url=avatar_url,
            banner_url=banner_url,
            description=description,
            short_description=short_description,
            prefix=prefix,
            website=website,
            support_server=support_server,
            invite_link=invite_link,
            tags=tags_str,
            owner_id=session["user"]["id"],
            owner_name=session["user"]["username"],
            added_date=datetime.now().isoformat(),
            upvotes=0,
            server_count=0,
            certified=False
        )
        db.session.add(new_bot)
        db.session.commit()

        flash("Bot added successfully!", "success")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    predefined_tags = [
        "Moderation", "Music", "Fun", "Utility", "Games", "Economy",
        "Leveling", "Logging", "Social", "Auto-Moderation", "Welcomer",
        "Tickets", "Analytics", "RPG", "Anime", "Memes", "NSFW",
        "Productivity", "Dashboard", "AI", "Crypto", "NFT", "Multipurpose"
    ]
    return render_template("add_bot.html", tags=predefined_tags, user=session.get("user"))

# --- Edit bot ---
@app.route("/edit-bot/<bot_id>", methods=["GET", "POST"])
def edit_bot(bot_id):
    if "user" not in session:
        flash("Please login to edit bots.", "error")
        return redirect(url_for("login"))

    bot = Bot.query.filter_by(id=bot_id).first()
    if not bot:
        flash("Bot not found.", "error")
        return redirect(url_for("index"))

    user = session.get("user")
    if not can_edit_bot(bot, user):
        flash("You don't have permission to edit this bot.", "error")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    if request.method == "POST":
        bot.description = request.form.get("description")
        bot.short_description = request.form.get("short_description")
        bot.prefix = request.form.get("prefix", "!")
        bot.website = request.form.get("website", "")
        bot.support_server = request.form.get("support_server", "")
        bot.invite_link = request.form.get("invite_link", "")
        bot.tags = ",".join(request.form.getlist("tags"))

        if is_admin(user):
            bot.certified = "certified" in request.form
            bot.server_count = int(request.form.get("server_count", 0))

        db.session.commit()

        flash("Bot updated successfully!", "success")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    predefined_tags = [
        "Moderation", "Music", "Fun", "Utility", "Games", "Economy",
        "Leveling", "Logging", "Social", "Auto-Moderation", "Welcomer",
        "Tickets", "Analytics", "RPG", "Anime", "Memes", "NSFW",
        "Productivity", "Dashboard", "AI", "Crypto", "NFT", "Multipurpose"
    ]
    bot_tags = bot.tags.split(",") if bot.tags else []
    return render_template("edit_bot.html",
        bot=bot,
        tags=predefined_tags,
        bot_tags=bot_tags,
        user=user,
        is_admin=is_admin(user)
    )

# --- Delete bot ---
@app.route("/delete-bot/<bot_id>", methods=["POST"])
def delete_bot(bot_id):
    if "user" not in session:
        flash("Please login to delete bots.", "error")
        return redirect(url_for("login"))

    bot = Bot.query.filter_by(id=bot_id).first()
    if not bot:
        flash("Bot not found.", "error")
        return redirect(url_for("index"))

    user = session.get("user")
    if not can_delete_bot(bot, user):
        flash("You don't have permission to delete this bot.", "error")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    db.session.delete(bot)
    db.session.commit()
    flash("Bot deleted successfully!", "success")
    return redirect(url_for("index"))

# --- Upvote bot with cooldown ---
@app.route("/upvote/<bot_id>", methods=["POST"])
def upvote_bot(bot_id):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    bot = Bot.query.filter_by(id=bot_id).first()
    if not bot:
        return jsonify({"error": "Bot not found"}), 404

    user_id = session["user"]["id"]
    now = datetime.now()
    key = (str(user_id), str(bot_id))

    last_vote = user_votes.get(key)
    if last_vote and now - last_vote < timedelta(hours=COOLDOWN_HOURS):
        cooldown_left = last_vote + timedelta(hours=COOLDOWN_HOURS) - now
        hours, remainder = divmod(cooldown_left.seconds, 3600)
        minutes = remainder // 60
        cooldown_msg = f"{hours}h {minutes}m"
        return jsonify({"error": f"Cooldown: next vote in {cooldown_msg}"}), 403

    bot.upvotes = (bot.upvotes or 0) + 1
    user_votes[key] = now
    db.session.commit()
    return jsonify({"upvotes": bot.upvotes}), 200

# --- Discord OAuth2 login ---
@app.route("/login")
def login():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify"
    }
    url = f"https://discord.com/api/oauth2/authorize?{urlencode(params)}"
    return redirect(url)

# --- OAuth2 callback ---
@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        flash("Authentication failed.", "error")
        return redirect(url_for("index"))

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": "identify"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)

    if r.status_code != 200:
        flash("Authentication failed.", "error")
        return redirect(url_for("index"))

    tokens = r.json()
    user_resp = requests.get("https://discord.com/api/users/@me", headers={
        "Authorization": f"Bearer {tokens['access_token']}"
    })

    if user_resp.status_code != 200:
        flash("Failed to fetch user data.", "error")
        return redirect(url_for("index"))

    session["user"] = user_resp.json()
    flash("Successfully logged in!", "success")
    return redirect(url_for("index"))

# --- Logout ---
@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out successfully.", "info")
    return redirect(url_for("index"))

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)