import asyncio
import json
import os
import random
import time
import sys
import subprocess
import signal
from datetime import datetime, timedelta
import traceback
from highrise import BaseBot, Position, AnchorPosition, Item
import aiohttp
try:
    from highrise import WebAPI
except ImportError:
    WebAPI = None

# Configure console for UTF-8 to prevent emoji-related crashes on Windows
try:
    if sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
except: pass

# ==================== RADIO SERVER CONFIG ====================
# Priority: RADIO_API_URL env var → Replit dev domain → localhost
_dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
_env_radio = os.environ.get("RADIO_API_URL", "").strip().rstrip("/")
if _env_radio:
    RADIO_API_URL = _env_radio
elif _dev_domain:
    RADIO_API_URL = f"https://{_dev_domain}"
else:
    RADIO_API_URL = "http://localhost:8080"
RADIO_STREAM_URL = f"{RADIO_API_URL}/api/radio/stream"
print(f"[RADIO] API URL: {RADIO_API_URL}")
# ============================================================


class BotRadio:
    """نظام راديو مدمج داخل البوت - يشغل مع البوت بدون سيرفر خارجي."""

    def __init__(self):
        self.queue = []      # [{title, url, uploader, duration, requested_by}]
        self.history = []    # previous songs
        self.current = None
        self.is_playing = False
        self._skip_event = None
        self._bot = None
        self._loop_task = None

    def set_bot(self, bot):
        self._bot = bot
        self._skip_event = asyncio.Event()

    async def _search(self, query):
        """Search YouTube using yt-dlp and return top results."""
        try:
            import sys, shutil
            ytdlp_cmd = shutil.which("yt-dlp") or shutil.which(
                "/home/container/.local/bin/yt-dlp"
            ) or [sys.executable, "-m", "yt_dlp"]
            if isinstance(ytdlp_cmd, str):
                ytdlp_cmd = [ytdlp_cmd]
            proc = await asyncio.create_subprocess_exec(
                *ytdlp_cmd,
                "--flat-playlist",
                "--no-warnings",
                "--dump-json",
                f"ytsearch5:{query}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)
            results = []
            for line in stdout.decode(errors="ignore").strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    url = data.get("url") or data.get("webpage_url") or ""
                    title = data.get("title") or data.get("fulltitle") or ""
                    uploader = data.get("uploader") or data.get("channel") or ""
                    duration = data.get("duration") or 0
                    if title and url:
                        if not url.startswith("http"):
                            url = f"https://www.youtube.com/watch?v={url}"
                        results.append({
                            "title": title,
                            "url": url,
                            "uploader": uploader,
                            "duration": float(duration) if duration else 0,
                        })
                except Exception:
                    continue
            print(f"[Radio] Search '{query}' → {len(results)} results")
            return results
        except Exception as e:
            print(f"[Radio] Search error: {e}")
            return []

    async def add(self, query, requested_by):
        """Search and add song to queue. Returns (song, was_already_playing) or None."""
        results = await self._search(query)
        if not results:
            return None
        song = results[0]
        song["requested_by"] = requested_by
        self.queue.append(song)
        was_playing = self.is_playing
        if not self.is_playing:
            self._loop_task = asyncio.create_task(self._play_loop())
        return song, was_playing

    async def _announce(self, song):
        if not self._bot:
            return
        dur = song.get("duration", 0)
        mins, secs = int(dur) // 60, int(dur) % 60
        dur_str = f"{mins}:{secs:02d}" if dur else "؟"
        try:
            await self._bot.highrise.chat(
                f"<#FF4500>🎵 الآن يعزف في {self._bot.room_name}!\n"
                f"<#FFD700>🎶 {song['title'][:50]}\n"
                f"<#00CED1>👤 {song.get('uploader','')[:30]}\n"
                f"<#FFFFFF>⏱ {dur_str}  |  طلب: @{song['requested_by']}\n"
                f"<#C0C0C0>🔗 {song['url']}"
            )
        except Exception as e:
            print(f"[Radio] Announce error: {e}")

    async def _play_loop(self):
        self.is_playing = True
        while self.queue:
            song = self.queue.pop(0)
            self.current = song
            self._skip_event.clear()
            await self._announce(song)
            self.history.append(song)
            if len(self.history) > 20:
                self.history.pop(0)
            duration = max(30, int(song.get("duration", 180)))
            try:
                await asyncio.wait_for(self._skip_event.wait(), timeout=duration)
            except asyncio.TimeoutError:
                pass
        self.current = None
        self.is_playing = False
        if self._bot:
            try:
                await self._bot.highrise.chat("<#C0C0C0>📻 انتهت القائمة. أضف أغاني بـ !p اسم الأغنية")
            except:
                pass

    def skip(self):
        """Skip current song."""
        if self._skip_event:
            self._skip_event.set()

    async def previous(self):
        """Go back to previous song."""
        if not self.history:
            return None
        prev = self.history.pop()
        if self.current:
            self.queue.insert(0, self.current)
        self.queue.insert(0, prev)
        self.skip()
        return prev

    def get_queue_text(self):
        if not self.queue and not self.current:
            return "<#C0C0C0>📻 القائمة فاضية\n<#FFFFFF>اضيف أغاني بـ !p اسم الأغنية"
        lines = []
        if self.current:
            lines.append(f"<#FF4500>🎵 يعزف الآن: {self.current['title'][:40]}")
        if self.queue:
            lines.append(f"<#FFD700>📋 القائمة ({len(self.queue)}):")
            for i, s in enumerate(self.queue[:5], 1):
                lines.append(f"<#FFFFFF>{i}. {s['title'][:35]}")
            if len(self.queue) > 5:
                lines.append(f"<#C0C0C0>...و {len(self.queue)-5} أغاني أخرى")
        return "\n".join(lines)


# ==================== PLAYLIST MANAGER ====================
import json as _json
import random as _random

PLAYLIST_FILE = os.path.join(os.path.dirname(__file__), "radio_playlists.json")

GENRE_KEYWORDS = {
    "rap": [
        "راب", "rap", "hip hop", "hiphop",
        "مروان بابلو", "marwan pablo",
        "wegz", "وجز",
        "اموند", "amoon", "amond", "amonde",
        "willy", "ويلي",
        "دياب", "diab",
        "مصطفى العقاد",
        "بيبو", "طه",
        "بلطي", "balti",
        "بلاكو", "blaxo",
    ],
    "shaabi": [
        "شعبي", "مهرجان", "مهرجانات",
        "بيتلو", "فيلو",
        "حمو بيكا", "احمد موزه", "احمد موزة",
        "سعد الصغير", "عمر كمال",
        "حكيم", "نجوى كرم",
        "عصام صاصا", "فيلو",
        "بندق", "شاكوش",
    ],
    "romantic": [
        "رومانسي", "حب", "غرام", "عشق", "romantic", "love",
        "قلبي", "يا حبيبي", "حبيبتي", "موال",
        "عمرو دياب", "تامر حسني", "تامر عاشور",
        "اليسا", "نانسي", "شيرين",
        "محمد حماقي", "حماقي",
        "اصالة", "اصاله",
        "رامي صبري", "هاني شاكر",
        "وائل كفوري", "كفوري",
        "سميرة سعيد",
    ],
    "sad": [
        "حزين", "دموع", "وداع", "فراق", "بكاء", "sad",
        "حنين", "وجع", "معاك", "بعيد", "خسرت",
        "مليش حد", "لوحدي",
    ],
    "old": [
        "عبد الحليم", "عبدالحليم",
        "فيروز",
        "ام كلثوم", "كلثوم",
        "وردة",
        "كاظم الساهر", "كاظم",
        "نجاة",
        "فايزة احمد", "فايزه",
        "classic", "كلاسيك", "قديم", "زمان",
        "عبد الوهاب", "محمد عبد الوهاب",
        "صباح",
        "سميرة توفيق",
        "ليلى مراد",
        "محمد فوزي",
        "شادية",
        "نجم",
    ],
    "khaleeji": [
        "خليجي", "khaleeji", "gulf",
        "سعودي", "اماراتي", "كويتي",
        "عبادي الجوهر", "عبادي",
        "ماجد المهندس",
        "محمد عبده",
        "راشد الماجد",
        "عبد المجيد عبد الله",
        "فهد الكبيسي",
        "حسين الجسمي",
    ],
    "english": [
        "english", "pop", "rock", "edm", "dance", "billboard",
        "drake", "weeknd", "beyonce", "rihanna", "taylor swift",
        "eminem", "adele", "bruno mars",
    ],
}

def _normalize_ar(text: str) -> str:
    text = text.lower()
    for ch in "أإآ":
        text = text.replace(ch, "ا")
    text = text.replace("ة", "ه")
    text = text.replace("ى", "ي")
    return text

def detect_genre(title: str, uploader: str) -> str:
    combined = _normalize_ar(title + " " + uploader)
    for genre, keywords in GENRE_KEYWORDS.items():
        for kw in keywords:
            if _normalize_ar(kw) in combined:
                return genre
    return "other"

class PlaylistManager:
    def __init__(self):
        self.playlists: dict = {}
        self.load()

    def load(self):
        try:
            if os.path.exists(PLAYLIST_FILE):
                with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                    self.playlists = _json.load(f)
        except Exception as e:
            print(f"[Playlist] Load error: {e}")
            self.playlists = {}

    def save(self):
        try:
            with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
                _json.dump(self.playlists, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Playlist] Save error: {e}")

    def add_song(self, title: str, url: str, uploader: str, duration: int):
        genre = detect_genre(title, uploader)
        if genre not in self.playlists:
            self.playlists[genre] = []
        existing_urls = {s["url"] for s in self.playlists[genre]}
        if url not in existing_urls:
            self.playlists[genre].append({"title": title, "url": url, "uploader": uploader, "duration": duration})
            self.save()
            return genre
        return genre

    def get_songs(self, genre: str) -> list:
        return self.playlists.get(genre, [])

    def get_all_songs(self) -> list:
        all_songs = []
        for songs in self.playlists.values():
            all_songs.extend(songs)
        return all_songs

    def list_genres(self) -> dict:
        return {g: len(songs) for g, songs in self.playlists.items() if songs}

    def genre_display_name(self, genre: str) -> str:
        names = {
            "rap": "🎤 راب",
            "shaabi": "🥁 شعبي",
            "romantic": "💖 رومانسي",
            "sad": "😢 حزين",
            "old": "🎙 قديم",
            "khaleeji": "🌙 خليجي",
            "english": "🎸 إنجليزي",
            "other": "🎵 متنوع",
        }
        return names.get(genre, f"🎵 {genre}")

radio_playlists = PlaylistManager()
# ==========================================================


class CustomWebAPI:
    def __init__(self, token=None):
        self.token = token
        print(f"[DEBUG] CustomWebAPI initializing with token: {token[:10] if token else 'None'}...")
        try:
            from highrise.webapi import WebAPI as RealWebAPI
            self.real_api = RealWebAPI()
            print(f"[DEBUG] Successfully initialized RealWebAPI")
            # Inject token if possible, though WebAPI usually relies on env vars or context
            # We will use the proper methods
        except ImportError as e:
            self.real_api = None
            print(f"[WARNING] Real WebAPI not found: {e}, using limited fallback.")
        except Exception as e:
            self.real_api = None
            print(f"[ERROR] Failed to initialize WebAPI: {e}")

    async def get_users(self, username: str, limit: int = 1):
        print(f"[DEBUG] CustomWebAPI.get_users called for username={username}, limit={limit}")
        print(f"[DEBUG] Using real_api: {self.real_api is not None}")
        
        if self.real_api:
            # Use the real WebAPI
            try:
                print(f"[DEBUG] Calling real_api.get_users...")
                response = await self.real_api.get_users(username=username, limit=limit)
                print(f"[DEBUG] real_api.get_users returned: {response}")
                return response
            except Exception as e:
                # Suppress 404 trace as it's common and handled by fallback
                if "404" not in str(e):
                    print(f"[DEBUG] WebAPI get_users hint: {e}")
                # Fallback to manual if needed
        
        # Use versioned API and include token for authorized access
        url = "https://webapi.highrise.game/v1/users"
        params = {"username": username, "limit": limit}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        
        class MockUserBasic:
            def __init__(self, uid, name):
                self.user_id = uid
                self.username = name
        
        class MockUsersResponse:
            def __init__(self, ulist):
                self.users = ulist

        async with aiohttp.ClientSession() as session:
            # Try Strategy 1: Versioned API
            async with session.get(url, params=params, headers=headers) as resp:
                print(f"[DEBUG] V1 Search Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    users_list = []
                    for u in data.get("users", []):
                         users_list.append(MockUserBasic(u.get("user_id"), u.get("username")))
                    
                    if users_list:
                        return MockUsersResponse(users_list)
                    # If empty list, fall through to Strategy 2
            
            # Strategy 2: Try unversioned API if Strat 1 failed or returned 0 results
            url_alt = "https://webapi.highrise.game/users"
            async with session.get(url_alt, params=params, headers=headers) as resp_alt:
                print(f"[DEBUG] Alt Search Status: {resp_alt.status}")
                if resp_alt.status == 200:
                    data = await resp_alt.json()
                    users_list = []
                    for u in data.get("users", []):
                         users_list.append(MockUserBasic(u.get("user_id"), u.get("username")))
                    if users_list:
                        return MockUsersResponse(users_list)

            class EmptyResponse:
                def __init__(self): self.users = []
            return EmptyResponse()

    async def get_user(self, user_id):
        if self.real_api:
            try:
                return await self.real_api.get_user(user_id)
            except Exception as e:
                if "404" not in str(e):
                    print(f"[DEBUG] WebAPI get_user hint: {e}")

        # Use versioned API for specific user details
        url = f"https://webapi.highrise.game/v1/users/{user_id}"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        
        async with aiohttp.ClientSession() as session:
            async def parse_response(r):
                if r.status != 200: return None
                d = await r.json()
                ud = d.get("user", d) # Support both structures
                if "user" in d and ud == d: ud = d["user"] 
                
                class MockOutfit:
                    def __init__(self, iid, pal, ab, itype):
                        self.item_id, self.active_palette, self.account_bound, self.type = iid, pal, ab, itype
                
                raw_outfit = ud.get("outfit", [])
                outfit = [MockOutfit(o.get("item_id"), o.get("active_palette", 0), o.get("account_bound", False), o.get("type", "clothing")) 
                          for o in raw_outfit]
                
                class MockUser:
                    def __init__(self, uid, name, out):
                        self.user_id, self.username, self.outfit = uid, name, out
                
                class MockResponse:
                    def __init__(self, u): self.user = u
                
                return MockResponse(MockUser(ud.get("user_id", user_id), ud.get("username", ""), outfit))

            async with session.get(url, headers=headers) as resp:
                print(f"[DEBUG] V1 Detail Status: {resp.status}")
                result = await parse_response(resp)
                if result: return result
                
                if resp.status == 404:
                    url_alt = f"https://webapi.highrise.game/users/{user_id}"
                    async with session.get(url_alt, headers=headers) as resp_alt:
                        print(f"[DEBUG] Alt Detail Status: {resp_alt.status}")
                        return await parse_response(resp_alt)
                return None
                
    async def get_user_by_username(self, username):
        """Helper to resolve a username to a full User Response including ID and Outfit."""
        try:
            # 1. Search for user to get ID
            search_resp = await self.get_users(username=username, limit=1)
            if search_resp and hasattr(search_resp, 'users') and search_resp.users:
                user_id = search_resp.users[0].user_id
                # 2. Get full details including outfit
                return await self.get_user(user_id)
        except Exception as e:
            print(f"[DEBUG] get_user_by_username error for {username}: {e}")
        return None

    async def get_outfit(self, user_id_or_name):
        """Fetch user outfit via Web API and convert to Highrise Items."""
        try:
            print(f"[DEBUG] Fetching outfit for: {user_id_or_name}")
            
            # Resolve to full user response
            if len(str(user_id_or_name)) < 15: # Likely a username or short ID
                user_response = await self.get_user_by_username(user_id_or_name)
                # If username search failed, try as direct ID
                if not user_response:
                    user_response = await self.get_user(user_id_or_name)
            else:
                user_response = await self.get_user(user_id_or_name)

            if not user_response:
                print(f"[DEBUG] Could not resolve user {user_id_or_name}")
                return None
            
            if not hasattr(user_response, 'user'):
                print(f"[DEBUG] user_response has no 'user' attribute")
                return None
            
            target_user = user_response.user
            print(f"[DEBUG] Resolved Cloud User: {target_user.username} (ID: {target_user.user_id})")
            
            web_outfit = target_user.outfit
            if not web_outfit:
                print(f"[DEBUG] User has empty outfit")
                return []

            print(f"[DEBUG] User has {len(web_outfit)} outfit items")

            # Convert WebAPI OutfitItem to Highrise Item
            game_items = []
            for wo in web_outfit:
                try:
                    # Robust field extraction
                    item_id = getattr(wo, 'item_id', None)
                    if not item_id and isinstance(wo, dict): item_id = wo.get("item_id")
                    if not item_id: continue
                    
                    palette = getattr(wo, 'active_palette', 0)
                    if palette is None and isinstance(wo, dict): palette = wo.get("active_palette", 0)
                    
                    # Try to get type otherwise default to clothing
                    item_type = getattr(wo, 'type', 'clothing')
                    if not item_type and isinstance(wo, dict): item_type = wo.get("type", "clothing")
                    
                    ab = getattr(wo, 'account_bound', True) # Default True for cloned items
                    
                    # Create Item with standard properties
                    # Highrise wears clothes, skin, hair, etc.
                    item = Item(
                        type=str(item_type) if item_type else "clothing",
                        amount=1,
                        id=str(item_id),
                        active_palette=int(palette) if palette is not None else 0
                    )
                    # Safely inject account_bound
                    try: setattr(item, 'account_bound', bool(ab))
                    except: pass
                    
                    game_items.append(item)
                except Exception as e:
                    print(f"[ERROR] Outfit conversion skip: {e}")
            
            print(f"[DEBUG] Converted {len(game_items)} items for bot wear.")
            return game_items

        except Exception as e:
            # Silent fallback if user not found, only log serious errors
            if "404" not in str(e):
                print(f"[ERROR] get_outfit error: {e}")
            return None
async def get_user_position(bot, user_id):
    """Finds the position of a specific user in the room."""
    try:
        room_users = (await bot.highrise.get_room_users()).content
        for user, position in room_users:
            if user.id == user_id:
                return position
    except Exception as e:
        print(f"Error getting user pos: {e}")
    return None

async def summon_user(bot, target, dest_position):
    """
    Teleports a single user to a destination position.
    Args:
        bot: The bot instance.
        target: The username or user_id to summon.
        dest_position: The Position or AnchorPosition to teleport to.
    Returns:
        bool: True if successful, False otherwise.
    """
    try:
        room_users = (await bot.highrise.get_room_users()).content
        target = target.replace("@", "").lower()
        
        target_id = None
        for user, pos in room_users:
            if user.username.lower() == target:
                target_id = user.id
                break
        
        if not target_id:
            return False

        if isinstance(dest_position, (Position, AnchorPosition)):
            await bot.highrise.teleport(target_id, dest_position)
            return True
            
    except Exception as e:
        print(f"Summon error: {e}")
    return False

async def summon_all(bot, dest_position, exclude_user_id=None):
    """
    Teleports all users in the room to a destination position.
    Args:
        bot: The bot instance.
        dest_position: The Position or AnchorPosition to teleport to.
        exclude_user_id: Optional user ID to exclude (e.g., the summoner).
    """
    try:
        # If it's an AnchorPosition, we try to convert to Position or just use it
        # Web SDK 2.x supports both in teleport
        
        print(f"[SummonAll] Starting teleport for all users to {dest_position}")
        response = await bot.highrise.get_room_users()
        room_users = response.content
        bot_id = getattr(bot, 'bot_id', None)
        
        count = 0
        for user, position in room_users:
            if user.id != bot_id and user.id != exclude_user_id:
                try:
                    # Teleport directly to the position
                    await bot.highrise.teleport(user.id, dest_position)
                    count += 1
                    # Added a small delay to prevent rate limit issues when summoning many people
                    await asyncio.sleep(0.1) 
                except Exception as e:
                    print(f"[SummonAll] Failed to teleport {user.username}: {e}")
                    pass
        print(f"[SummonAll] Teleported {count} users.")
        return True
    except Exception as e:
        print(f"Summon all error: {e}")
        import traceback
        traceback.print_exc()
        return False


class MyBot(BaseBot):
    def __init__(self):
        super().__init__()
        self.is_shutting_down = False
        self.load_config()

    def load_config(self):
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
                self.room_id = config.get("room_id", "")
                self.bot_token = config.get("bot_token", "")
        except Exception:
            self.room_id = ""
            self.bot_token = ""

    rizz_lines = [
        "Are you a magician? Because whenever I look at you, everyone else disappears.",
        "Are you a camera? Because every time I look at you, I smile.",
        "Do you have a map? because I just got lost in your eyes.",
        "Are you from France? Because Eiffel for you.",
        "If you were a vegetable, you'd be a cute-cumber."
    ]
    roast_lines = [
        "You're the reason the gene pool needs a lifeguard.",
        "I'd agree with you, but then we'd both be wrong.",
        "You're like a cloud. When you disappear, it's a beautiful day.",
        "Is your drama over? No? That's fine, I'll wait for the sequel.",
        "You're not stupid; you just have bad luck when it comes to thinking."
    ]
    async def on_start(self, session_metadata):
        print("Bot connection started!")
        self.bot_id = session_metadata.user_id
        self.start_time = time.time()
        self.loop_task = None
        # Capture config from environment and arguments
        self.bot_name = os.environ.get("BOT_NAME", "Unknown")
        try:
            self.room_id = sys.argv[2]
            self.bot_token = sys.argv[3] if len(sys.argv) > 3 else None
        except Exception:
            self.room_id = "unknown"
            self.bot_token = None
        self.pending_bot_setup = {}
        # Initialize Web API safely
        # Always use CustomWebAPI to guarantee it works as user requested "add webapi"
        # and complained about missing one.
        try:
             self.webapi = CustomWebAPI(token=self.bot_token)
             print("WebAPI initialized (Custom implementation).")
        except Exception as e: 
             print(f"Could not init WebAPI: {e}")
             self.webapi = None
        # Load per-bot config from bots_config.json (room_name, radio_port)
        self.room_name = "مقزار روم"
        radio_port = 8080
        try:
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots_config.json")
            if not os.path.exists(cfg_path):
                cfg_path = "bots_config.json"
            with open(cfg_path, "r", encoding="utf-8") as _f:
                _bots_cfg = json.load(_f)
            for _b in _bots_cfg:
                if _b.get("room_id") == self.room_id:
                    self.room_name = _b.get("room_name", self.room_name)
                    radio_port = int(_b.get("radio_port", radio_port))
                    break
        except Exception as _e:
            print(f"[WARN] Could not load per-bot config: {_e}")
        # Radio System URLs
        self.RADIO_API_URL = f"http://localhost:{radio_port}"
        self.RADIO_STREAM_URL = RADIO_STREAM_URL

        # Admin System
        self.hardcoded_owners = ["to_xic_"]  # Define hardcoded owners first
        self.OWNERS = ["to_xic_"]
        # Ensure settings directory exists
        if not os.path.exists("settings"):
            os.makedirs("settings")
        self.settings_file = os.path.join("settings", f"settings_{self.room_id}.json")
        
        # Migrate old settings if they exist and new one doesn't
        if not os.path.exists(self.settings_file) and os.path.exists(os.path.join("settings", "settings.json")):
            import shutil
            try:
                shutil.copy(os.path.join("settings", "settings.json"), self.settings_file)
                print(f"Migrated settings.json to {self.settings_file}")
            except Exception as e:
                print(f"Migration error: {e}")
        self.load_settings()

        # Load saved owners
        saved_owners = self.settings.get("owners", [])
        self.OWNERS = list(set(self.hardcoded_owners + saved_owners))
        
        # Blocked Admins System
        # Blocked Users System (Prevents command usage)
        self.blocked_users = self.settings.get("blocked_users", [])
        
        # Merge owners with saved admins
        saved_admins = self.settings.get("admins", [])
        self.ADMINS = list(set(self.OWNERS + saved_admins))
        
        # VIP System
        self.VIPS = self.settings.get("vips", [])
        
        self.total_tips = self.settings.get("total_tips", 0)
        self.tip_history = self.settings.get("tip_history", []) # List of {"username": str, "amount": int, "time": str}
        self.vip_cost = self.settings.get("vip_cost", 1000) # Default 1000 gold
        self.vip_cost_30d = self.settings.get("vip_cost_30d", 500)
        self.vip_cost_90d = self.settings.get("vip_cost_90d", 1200)
        self.vip_cost_perm = self.settings.get("vip_cost_perm", 5000)
        self.user_tips_ledger = self.settings.get("user_tips_ledger", {}) # {username: total_gold}
        
        
        # Language System
        self.language = self.settings.get("language", "english")
        
        
        
        # User Visit Tracking System
        # Format: {"room_id": {"username": visit_count}}
        self.user_visits = self.settings.get("user_visits", {})

        # Ban System
        self.banned_users = self.settings.get("banned_users", {})

        # Radio Ticket System
        # Format: {"username": ticket_count}
        self.radio_tickets = self.settings.get("radio_tickets", {})
        # Pending playlist confirmations: {"username": {"genre": ..., "songs": [...]}}
        self.pending_playlist: dict = {}
        



        # Profile/Activity tracking
        self.command_usage = self.settings.get("command_usage", {})
        self.chat_stats = self.settings.get("chat_stats", {}) # {username: message_count}
        self.user_stats = self.settings.get("user_stats", {}) # {"username": {"first_seen": timestamp, "last_seen": timestamp}}
        
        # Level System
        # Format: {"username": {"xp": 0, "level": 1, "messages": 0, "time_spent": 0}}
        self.user_levels = self.settings.get("user_levels", {})

        # Streak System
        # Format: {"username": {"streak": 1, "last_seen": "YYYY-MM-DD"}}
        self.user_streaks = self.settings.get("user_streaks", {})

        # Emote System
        self.emotes = {}
        self.friendly_emotes = {}
        try:
            if os.path.exists(os.path.join("data", "emotes.json")):
                with open(os.path.join("data", "emotes.json"), "r") as f:
                    self.emotes = json.load(f)
                self.emote_list = list(self.emotes.keys())
                for key in self.emotes:
                    friendly = key.replace("emote-dance-", "").replace("emote-", "").lower()
                    self.friendly_emotes[friendly] = key
            else:
                print("emotes.json not found!")
                self.emote_list = []
        except Exception as e:
            print(f"Error loading emotes: {e}")
            self.emote_list = []
            
        # Bot Specific Emotes (for non-stop loop and expanded coverage)
        self.bot_emotes_data = {}
        self.bot_loop_emotes = []
        self.emote_duration_map = {}  # emote_id -> duration in seconds
        try:
            # Prefer the full backup if available, else use the simple one
            for b_file in ["bot_emotes_full.json", "bot_emotes.json", "emotes.json"]:
                target_file = os.path.join("data", b_file)
                if os.path.exists(target_file):
                    with open(target_file, "r", encoding="utf-8") as f:
                        self.bot_emotes_data = json.load(f)
                    
                    # Merge into main emotes list for chat triggers
                    for name, info in self.bot_emotes_data.items():
                        eid = info.get("id") if isinstance(info, dict) else info
                        if not eid: continue
                        
                        self.emotes[name] = eid
                        # Add friendly name too
                        friendly = name.replace("emote-dance-", "").replace("emote-", "").lower()
                        self.friendly_emotes[friendly] = eid

                    # Use all keys for the loop (including numbers is fine for internal logic)
                    # But for the DISPLAY list, we strip numbers to keep it clean
                    self.bot_loop_emotes = [k for k in self.bot_emotes_data.keys() if not k.isdigit()]
                    print(f"Loaded {len(self.bot_loop_emotes)} loopable emotes from {b_file}")

                    # Build reverse map: emote_id -> duration (in seconds)
                    self.emote_duration_map = {}
                    for _name, _info in self.bot_emotes_data.items():
                        if isinstance(_info, dict):
                            _eid = _info.get("id")
                            _dur = _info.get("duration", 0)
                            if _eid and _dur:
                                self.emote_duration_map[_eid] = float(_dur)
                    break
            
            # Update main emote list for indexing/display using only named emotes
            # This ensures that !1, !2 etc index correctly into a clean named list
            self.emote_list = sorted([k for k in self.emotes.keys() if not k.isdigit()])
            if not self.bot_loop_emotes:
                self.bot_loop_emotes = self.emote_list.copy()

        except Exception as e:
            print(f"Error loading bot loop emotes: {e}")
            self.bot_loop_emotes = []

        # Saved Outfits
        self.saved_outfits = self.settings.get("saved_outfits", {})


        self.dance_floor_mode = False # Default to False (Use ALL Emotes)
        self.following_user = None # User to follow
        self.room_moderators = set()  # Auto-detected moderators/designers from room privilege
        asyncio.create_task(self.run_follow_loop())
        asyncio.create_task(self._scan_room_moderators())

        # Radio System
        self.radio = BotRadio()
        self.radio.set_bot(self)
        
        # Store current room ID (will be set on first user join)
        self.current_room_id = None
        self.current_room_name = self.settings.get("room_name", "Welcome")  # Load from settings or default to "Welcome"
        
        # Location System (location.json)
        self.location_file = os.path.join("data", "location.json")
        self.locations = {}
        if os.path.exists(self.location_file):
            try:
                with open(self.location_file, "r") as f:
                    self.locations = json.load(f)
            except:
                self.locations = {}
        
        
        # Load permanent flash users
        saved_flash = self.settings.get("flash_users", [])
        self.flash_users = set(saved_flash)
        
        self.frozen_users = {} # {user_id: position}
        
        self.translations = {
            "english": {
                "welcome": "",
                "leave": "",
                "ping": "<#00FF00>Pong! 🏓 | <#00FFFF>Speed: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>Uptime: <#FFFF00>{d}d {h}h {m}m {s}s",
                "admins_only": "<#FF0000>Admins only!",
                "lang_set": "<#00FF00>Language set to English! 🇺🇸",
                "vip_list": "<#00FFFF>👑 VIP List: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>No VIPs set yet.",
                "role_sum": "<#00FFFF>🛡️ Bot Roles Summary:\n<#FFFF00>👤 Owners: <#FFFFFF>{o}\n<#FFFF00>👑 Admins: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>Bot Wallet Balance: <#FFFF00>{g} Gold",
                "inv_role": "<#FF0000>Invalid role! Use: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>No {role} found.",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>⚠️ Type !sub to unlock everything!\n<#FFFF00>🔹 !sub - <#00FF00>Unlock\n<#FFFF00>🔹 !ping\n<#FFFF00>🔹 !user / !profile / !lb\n<#FFFF00>🔹 !emotelist / !level / !xp",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz / !roast / !flirt\n<#FFFF00>🔹 !joke / !shayari\n<#FFFF00>🔹 !love / !hate\n<#FFFF00>🔹 !lovepercent\n<#FFFF00>🔹 !deathyear\n<#FFFF00>🔹 !stop\n<#FFFF00>🔹 !back",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MODERATOR COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban / !unban\n<#FFFF00>🔸 !summon / !come / !alleemotes\n<#FFFF00>🔸 !dance / !stopdance / !stopemotes\n<#FFFF00>🔸 !setdancefloor 1/2/on/off\n<#FFFF00>🔸 !set / !setroomname\n<#FFFF00>🔸 !setjoin [message]\n<#FFFF00>🔸 !tip / !tipall / !tipme\n<#FFFF00>🔸 !autotip on/off/status/time/amount\n<#FFFF00>🔸 !sublist\n<#FFFF00>🔸 !vipstatus @username\n<#FFFF00>🔸 !block / !unblock\n<#FFFF00>🔸 !cleartime / !clearchat\n<#FFFF00>🔸 !cashout / !restartbot / !clear data\n<#FFFF00>🔸 !adminlist / !history\n<#FFFF00>🔸 !timeon/off\n<#FFFF00>🔸 !adminmessage [on/off]\n<#FFFF00>🔸 !ownermessage [on/off]\n<#FFFF00>🔸 !startloop / !stoploop",
                "m_tele": "<#00FFFF>📍 TELEPORT COMMANDS 📍\n<#FFFF00>🔹 !telelist <#800080>- View all spots\n<#FFFF00>🔹 !create tele [name] <#800080>- Public spot\n<#FFFF00>🔹 !createvip tele [name] <#800080>- VIP spot\n<#FFFF00>🔹 !createmod tele [name] <#800080>- Mod spot\n<#FFFF00>🔹 !createowner tele [name] <#800080>- Owner spot\n<#FFFF00>🔹 !remtele [name] <#800080>- Delete spot\n<#FFFF00>🔹 !cleartele <#800080>- Clear all\n<#FFFF00>🔹 [name] <#800080>- Teleport to spot",
                "m_intro": "<#FF00FF>🤖 **COMMAND CATALOG** 🤖\n<#800080>━━━━━━━━━━━━━━━━━━━━━\n<#FFFF00>Type <#FFFFFF>!help [category] <#FFFF00>for info:\n<#00FFFF>🔹 !help public <#800080>- General\n<#00FFFF>🔹 !help fun <#800080>- Fun\n<#00FFFF>🔹 !help vip <#800080>- VIP stuff\n<#00FFFF>🔹 !help teleports <#800080>- Flash spots\n<#00FFFF>🔹 !help giveaway <#800080>- Giveaways\n<#00FFFF>🔹 !help reaction <#800080>- Rxns\n<#00FFFF>🔹 !help moderator <#800080>- Admin\n<#00FFFF>🔹 !help owner <#800080>- Owners\n<#800080>━━━━━━━━━━━━━━━━━━━━━\n<#FF00FF>🔗 Type !sub to unlock all!"
            },
            "hindi": {
                "welcome": "<#FF69B4>नमस्ते @{username} रूम में आपका स्वागत है",
                "leave": "👋 अपना ख्याल रखें <#FFFFFF>@{username} <#FF69B4>फिर मिलते हैं! ✨",
                "ping": "<#00FF00>पोंग! 🏓 | <#00FFFF>गति: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>अपटाइम: <#FFFF00>{d} दिन {h} घंटे {m} मिनट {s} सेकंड",
                "admins_only": "<#FF0000>केवल एडमिन के लिए!",
                "lang_set": "<#00FF00>भाषा हिंदी में सेट की गई! 🇮🇳",
                "vip_list": "<#00FFFF>👑 VIP सूची: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>कोई VIP सेट नहीं है।",
                "role_sum": "<#00FFFF>🛡️ बॉट भूमिका सारांश:\n<#FFFF00>👤 मालिक: <#FFFFFF>{o}\n<#FFFF00>👑 एडमिन: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>बॉट वॉलेट: <#FFFF00>{g} गोल्ड",
                "inv_role": "<#FF0000>अमान्य भूमिका! उपयोग करें: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>कोई {role} नहीं मिला।",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ सार्वजनिक आदेश ✨\n<#FF69B4>⚠️ सब कुछ अनलॉक करने के लिए !sub लिखें\n<#FFFF00>🔹 !sub - <#00FF00>अनलॉक\n<#FFFF00>🔹 !ping\n<#FFFF00>🔹 !user / !profile / !lb\n<#FFFF00>🔹 !emotelist / !level / !xp",
                "m_fun": "<#FF69B4>🎭 मजेदार आदेश 🎭\n<#FFFF00>🔹 !rizz / !shayari\n<#FFFF00>🔹 !love / !hate\n<#FFFF00>🔹 !deathyear\n<#FFFF00>🔹 !stop / !back",
                "m_vip": "<#FFD700>👑 वीआईपी आदेश 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ मॉडरेटर आदेश 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !tip / !tipall\n<#FFFF00>🔸 !vipstatus @username",
                "m_tele": "<#00FFFF>📍 टेलीपोर्ट आदेश 📍\n<#FFFF00>🔹 !telelist <#800080>- सभी स्थान देखें\n<#FFFF00>🔹 !create tele [नाम] <#800080>- सार्वजनिक स्थान\n<#FFFF00>🔹 !createvip tele [नाम] <#800080>- वीआईपी स्थान\n<#FFFF00>🔹 !createmod tele [नाम] <#800080>- मॉड स्थान\n<#FFFF00>🔹 !createowner tele [नाम] <#800080>- मालिक स्थान\n<#FFFF00>🔹 [नाम] <#800080>- स्थान पर टेलीपोर्ट करें",
                "m_intro": "<#FF00FF>🤖 **आदेश सूची (HELP)** 🤖\n<#800080>━━━━━━━━━━━━━━━━━━━━━\n<#FFFF00>विवरण के लिए <#FFFFFF>!help [श्रेणी] <#FFFF00>लिखें:\n<#00FFFF>🔹 !help public <#800080>- सार्वजनिक\n<#00FFFF>🔹 !help fun <#800080>- मनोरंजन\n<#00FFFF>🔹 !help vip <#800080>- वीआईपी\n<#00FFFF>🔹 !help teleports <#800080>- टेलीपोर्ट\n<#00FFFF>🔹 !help moderator <#800080>- एडमिन\n<#00FFFF>🔹 !help owner <#800080>- मालिक\n<#800080>━━━━━━━━━━━━━━━━━━━━━\n<#FF00FF>🔗 सब कुछ अनलॉक करने के लिए !sub लिखें!"
            },
            "spanish": {
                "welcome": "<#FF69B4>Hola @{username} bienvenido a la sala",
                "leave": "👋 ¡Cuídate <#FFFFFF>@{username} <#FF69B4>nos vemos luego! ✨",
                "ping": "<#00FF00>¡Pong! 🏓 | <#00FFFF>Velocidad: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>Tiempo activo: <#FFFF00>{d}d {h}h {m}m {s}s",
                "admins_only": "<#FF0000>¡Solo administradores!",
                "lang_set": "<#00FF00>¡Idioma configurado en español! 🇪🇸",
                "vip_list": "<#00FFFF>👑 Lista VIP: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>No hay VIPs establecidos.",
                "role_sum": "<#00FFFF>🛡️ Resumen de Roles:\n<#FFFF00>👤 Dueños: <#FFFFFF>{o}\n<#FFFF00>👑 Admins: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>Billetera Bot: <#FFFF00>{g} Oro",
                "inv_role": "<#FF0000>Rol inválido. Usa: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>No se encontró {role}.",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ COMANDOS PÚBLICOS ✨\n<#FF69B4>⚠️ ¡Escribe !sub para desbloquear!\n<#FFFF00>🔹 !sub - <#00FF00>Desbloquear\n<#FFFF00>🔹 !ping\n<#FFFF00>🔹 !user / !info",
                "m_fun": "<#FF69B4>🎭 DIVERSIÓN 🎭\n<#FFFF00>🔹 !rizz / !flirt\n<#FFFF00>🔹 !love / !hate\n<#FFFF00>🔹 !deathyear",
                "m_vip": "<#FFD700>👑 COMANDOS VIP 👑\n<#FFFF00>🔹 !vipcost / !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MODERACIÓN 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !tip / !tipall\n<#FFFF00>🔸 !vipstatus @username",
            },
            "arabic": {
                "welcome": "",
                "ping": "<#00FF00>بونج! 🏓 | <#00FFFF>السرعة: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>وقت التشغيل: <#FFFF00>{d} يوم {h} ساعة {m} دقيقة {s} ثانية",
                "admins_only": "<#FF0000>للمشرفين فقط!",
                "lang_set": "<#00FF00>تم ضبط اللغة على العربية! 🇸🇦",
                "vip_list": "<#00FFFF>👑 قائمة VIP: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>لم يتم تعيين VIP بعد.",
                "role_sum": "<#00FFFF>🛡️ ملخص رتب البوت:\n<#FFFF00>👤 المالكين: <#FFFFFF>{o}\n<#FFFF00>👑 المشرفين: <#FFFFFF>{a}\n<#FFFF00>💎 VIP: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>رصيد محفظة البوت: <#FFFF00>{g} ذهب",
                "inv_role": "<#FF0000>رتبة غير صالحة! استخدم: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>لم يتم العثور على {role}.",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ أوامر عامة ✨\n<#FF69B4>⚠️ اكتب !sub لفتح الأوامر\n<#FFFF00>🔹 !sub - <#00FF00>فتح\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 ترفيه 🎭\n<#FFFF00>🔹 !rizz / !flirt\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 أوامر VIP 👑\n<#FFFF00>🔹 !vipcost / !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ إشراف 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "punjabi": {
                "welcome": "<#FF69B4>ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ @{username}, ਤੁਹਾਡਾ ਸੁਆਗਤ ਹੈ!",
                "ping": "<#00FF00>ਪੋਂਗ! 🏓 | <#00FFFF>ਸਪੀਡ: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>ਸਮਾਂ: <#FFFF00>{d} ਦਿਨ {h} ਘੰਟੇ {m} ਮਿੰਟ {s} ਸੈਕਿੰਡ",
                "admins_only": "<#FF0000>ਸਿਰਫ ਐਡਮਿਨ ਲਈ!",
                "lang_set": "<#00FF00>ਭਾਸ਼ਾ ਪੰਜਾਬੀ ਵਿੱਚ ਸੈਟ ਕੀਤੀ ਗਈ! 🇮🇳",
                "vip_list": "<#00FFFF>👑 VIP ਸੂਚੀ: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>ਕੋਈ VIP ਨਹੀਂ ਹੈ।",
                "role_sum": "<#00FFFF>🛡️ ਰੋਲ ਸਾਰ:\n<#FFFF00>👤 ਮਾਲਕ: <#FFFFFF>{o}\n<#FFFF00>👑 ਐਡਮਿਨ: <#FFFFFF>{a}\n<#FFFF00>💎 VIP: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>ਬੋਟ ਵਾਲਿਟ: <#FFFF00>{g} ਗੋਲਡ",
                "inv_role": "<#FF0000>ਅਵੈਧ ਰੋਲ! ਵਰਤੋ: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>ਕੋਈ {role} ਨਹੀਂ ਮਿਲਿਆ।",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "marathi": {
                "welcome": "<#FF69B4>नमस्कार @{username} रूममध्ये तुमचे स्वागत आहे",
                "ping": "<#00FF00>पोंग! 🏓 | <#00FFFF>वेग: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>वेळ: <#FFFF00>{d} दिवस {h} तास {m} मिनिटे {s} सेकंद",
                "admins_only": "<#FF0000>फक्त ॲडमिनसाठी!",
                "lang_set": "<#00FF00>भाषा मराठीत सेट केली आहे! 🇮🇳",
                "vip_list": "<#00FFFF>👑 VIP यादी: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>अजून एकही VIP सेट नाही.",
                "role_sum": "<#00FFFF>🛡️ बॉट भूमिका सारांश:\n<#FFFF00>👤 मालक: <#FFFFFF>{o}\n<#FFFF00>👑 ॲडमिन: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>बॉट वॉलेट शिल्लक: <#FFFF00>{g} गोल्ड",
                "inv_role": "<#FF0000>अवैध भूमिका! वापरा: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>कोणताही {role} सापडला नाही।",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "turkish": {
                "welcome": "<#FF69B4>Merhaba @{username} odaya hoş geldiniz",
                "ping": "<#00FF00>Pong! 🏓 | <#00FFFF>Hız: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>Çalışma Süresi: <#FFFF00>{d}g {h}sa {m}dk {s}sn",
                "admins_only": "<#FF0000>Sadece yöneticiler!",
                "lang_set": "<#00FF00>Dil Türkçe olarak ayarlandı! 🇹🇷",
                "vip_list": "<#00FFFF>👑 VIP Listesi: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>Henüz bir VIP ayarlanmadı.",
                "role_sum": "<#00FFFF>🛡️ Bot Rolleri Özeti:\n<#FFFF00>👤 Sahipler: <#FFFFFF>{o}\n<#FFFF00>👑 Yöneticiler: <#FFFFFF>{a}\n<#FFFF00>💎 VIP'ler: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>Bot Cüzdan Bakiyesi: <#FFFF00>{g} Altın",
                "inv_role": "<#FF0000>Geçersiz rol! Şunu kullanın: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>{role} bulunamadı.",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "bengali": {
                "welcome": "<#FF69B4>হ্যালো @{username} রুমে আপনাকে স্বাগতম",
                "ping": "<#00FF00>পং! 🏓 | <#00FFFF>গতি: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>সময়: <#FFFF00>{d} দিন {h} ঘন্টা {m} মিনিট {s} সেকেন্ড",
                "admins_only": "<#FF0000>শুধুমাত্র অ্যাডমিনদের জন্য!",
                "lang_set": "<#00FF00>ভাষা বাংলায় সেট করা হয়েছে! 🇧🇩",
                "vip_list": "<#00FFFF>👑 ভিআইপি তালিকা: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>কোনো ভিআইপি নেই।",
                "role_sum": "<#00FFFF>🛡️ রোলের সারাংশ:\n<#FFFF00>👤 মালিক: <#FFFFFF>{o}\n<#FFFF00>👑 অ্যাডমিন: <#FFFFFF>{a}\n<#FFFF00>💎 ভিআইপি: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>বট ওয়ালেট: <#FFFF00>{g} গোল্ড",
                "inv_role": "<#FF0000>অবৈধ রোল! ব্যবহার করুন: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>কোন {role} পাওয়া যায়নি।",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "chinese": {
                "welcome": "<#FF69B4>你好 @{username} 欢迎来到房间",
                "ping": "<#00FF00>乒! 🏓 | <#00FFFF>速度: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>运行时间: <#FFFF00>{d}天 {h}小时 {m}分 {s}秒",
                "admins_only": "<#FF0000>仅限管理员!",
                "lang_set": "<#00FF00>语言已设置为中文! 🇨🇳",
                "vip_list": "<#00FFFF>👑 VIP 列表: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>尚未设置 VIP。",
                "role_sum": "<#00FFFF>🛡️ 机器人角色摘要:\n<#FFFF00>👤 拥有者: <#FFFFFF>{o}\n<#FFFF00>👑 管理员: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>机器人钱包余额: <#FFFF00>{g} 金币",
                "inv_role": "<#FF0000>无效角色！使用：!rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>未找到 {role}。",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "japanese": {
                "welcome": "<#FF69B4>こんにちは @{username} ルームへようこそ",
                "ping": "<#00FF00>ポン! 🏓 | <#00FFFF>速度: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>稼働時間: <#FFFF00>{d}日 {h}時間 {m}分 {s}秒",
                "admins_only": "<#FF0000>管理者のみ!",
                "lang_set": "<#00FF00>言語が日本語に設定されました! 🇯🇵",
                "vip_list": "<#00FFFF>👑 VIPリスト: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>VIPはまだ設定されていません。",
                "role_sum": "<#00FFFF>🛡️ ボットの役割の概要:\n<#FFFF00>👤 オーナー: <#FFFFFF>{o}\n<#FFFF00>👑 管理者: <#FFFFFF>{a}\n<#FFFF00>💎 VIP: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>ボットのウォレット残高: <#FFFF00>{g} ゴールド",
                "inv_role": "<#FF0000>無効な役割です。使用法: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>{role} が見つかりませんでした。",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "german": {
                "welcome": "<#FF69B4>Hallo @{username} willkommen im Raum",
                "ping": "<#00FF00>Pong! 🏓 | <#00FFFF>Geschwindigkeit: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>Laufzeit: <#FFFF00>{d}t {h}std {m}min {s}sek",
                "admins_only": "<#FF0000>Nur für Admins!",
                "lang_set": "<#00FF00>Sprache auf Deutsch eingestellt! 🇩🇪",
                "vip_list": "<#00FFFF>👑 VIP-Liste: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>Noch keine VIPs festgelegt.",
                "role_sum": "<#00FFFF>🛡️ Bot-Rollen Zusammenfassung:\n<#FFFF00>👤 Besitzer: <#FFFFFF>{o}\n<#FFFF00>👑 Admins: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>Bot Wallet Balance: <#FFFF00>{g} Gold",
                "inv_role": "<#FF0000>Ungültige Rolle! Benutze: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>Kein {role} gefunden.",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ PUBLIC COMMANDS ✨\n<#FF69B4>Type !sub first!\n<#FFFF00>🔹 !sub\n<#FFFF00>🔹 !ping",
                "m_fun": "<#FF69B4>🎭 FUN COMMANDS 🎭\n<#FFFF00>🔹 !rizz\n<#FFFF00>🔹 !love / !hate",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ MOD COMMANDS 🛡️\n<#FFFF00>🔸 !kick / !ban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !vipstatus @username"
            },
            "french": {
                "welcome": "<#FF69B4>Bonjour @{username} bienvenue dans la salle",
                "ping": "<#00FF00>Pong! 🏓 | <#00FFFF>Vitesse: <#FFFF00>{latency}ms",
                "uptime": "<#00FF00>Temps d'activité: <#FFFF00>{d}j {h}h {m}min {s}s",
                "admins_only": "<#FF0000>Réservé aux admins!",
                "lang_set": "<#00FF00>Langue réglée sur le Français! 🇫🇷",
                "vip_list": "<#00FFFF>👑 Liste VIP: <#FF69B4>{vips}",
                "no_vips": "<#FFFF00>Aucun VIP défini.",
                "role_sum": "<#00FFFF>🛡️ Résumé des rôles:\n<#FFFF00>👤 Propriétaires: <#FFFFFF>{o}\n<#FFFF00>👑 Admins: <#FFFFFF>{a}\n<#FFFF00>💎 VIPs: <#FFFFFF>{v}",
                "wallet": "💰 <#00FFFF>Solde du Bot: <#FFFF00>{g} Gold",
                "inv_role": "<#FF0000>¡Rol inválido! Usa: !rolelist admin/vip/owner",
                "no_mem": "<#FFFF00>No se encontró {role}.",
                "role_mem": "👥 <#00FFFF>{role}: <#FF69B4>{members}",
                "m_pub": "<#00FFFF>✨ COMMANDES PUBLIQUES ✨\n<#FF69B4>⚠️ Type !sub to unlock all commands!\n<#FFFF00>🔹 !sub - <#00FF00>Unlock Commands\n<#FFFF00>🔹 !lb / !lb2 - <#00FF00>Leaderboards\n<#FFFF00>🔹 !mytime / !time - <#00FF00>Activity\n<#FFFF00>🔹 !ping\n<#FFFF00>🔹 !user / !info / !rolelist\n<#FFFF00>🔹 !emotelist\n<#FFFF00>🔹 !giveaway [enter]",
                "m_fun": "<#FF69B4>🎭 COMMANDES FUN 🎭\n<#FFFF00>🔹 !rizz / !roast / !flirt\n<#FFFF00>🔹 !joke / !shayari\n<#FFFF00>🔹 !love / !hate\n<#FFFF00>🔹 !deathyear\n<#FFFF00>🔹 !stop\n<#FFFF00>🔹 !back",
                "m_vip": "<#FFD700>👑 VIP COMMANDS 👑\n<#FFFF00>🔹 !vipcost\n<#FFFF00>🔹 !viplist\n<#FFFF00>🔹 !buyvip",
                "m_mod": "<#00FF00>🛡️ COMMANDES MODO 🛡️\n<#FFFF00>🔸 !kick / !ban / !unban\n<#FFFF00>🔸 !summon / !come\n<#FFFF00>🔸 !set / !setroomname\n<#FFFF00>🔸 !tip / !tipall / !tipme\n<#FFFF00>🔸 !sublist\n<#FFFF00>🔸 !vipstatus @username\n<#FFFF00>🔸 !block / !unblock\n<#FFFF00>🔸 !cleartime / !clearchat\n<#FFFF00>🔸 !cashout / !restartbot / !clear data\n<#FFFF00>🔸 !chaton/off\n<#FFFF00>🔸 !timeon/off"
            }
        }
        



        # Tech Features
        self.following_user = None

        # Broadcast settings
        self.broadcast_message = self.settings.get("broadcast_msg", "")
        self.broadcast_interval = self.settings.get("broadcast_interval", 300) # 5 minutes
        self.subscribers = self.settings.get("subscribers", []) # List of subscriber usernames
        # self.broadcast_task = asyncio.create_task(self.auto_broadcast())

        # Systems toggles
        self.chat_tracking = self.settings.get("chat_tracking", False)
        self.time_tracking = self.settings.get("time_tracking", False)
        self.user_times = self.settings.get("user_times", {}) # Total seconds per user
        self.join_times = {} # Session start times
        self.looping_users = {} # {user_id: emote_id} for non-stop emotes
        self.loop_task = asyncio.create_task(self.run_emote_loop())
        
        # Auto-Tip System
        self.auto_tip = self.settings.get("auto_tip", False)
        self.autotip_interval = self.settings.get("autotip_interval", 600)
        self.autotip_amount = self.settings.get("autotip_amount", 1)
        self.autotip_task = asyncio.create_task(self.run_auto_tip())

        # Loop Message System
        # Format: {"1": "message1", "2": "message2", ...}
        self.loop_messages = self.settings.get("loop_messages", {})
        self.loop_intervals = self.settings.get("loop_intervals", {}) # Per-slot intervals
        self.loop_cooldown = self.settings.get("loop_cooldown", 60)  # Default 60 seconds
        self.loop_running = self.settings.get("loop_running", False)
        self.loop_task_handle = asyncio.create_task(self.run_loop_messages())
        # All Emotes Loop System
        self.playing_all_emotes = True
        self.emote_loop_task = None
        
        asyncio.create_task(self.run_all_emotes_loop())
        
        # Optimization: Periodic Saving
        self.settings_dirty = False
        asyncio.create_task(self.run_periodic_save())

        # Flash Teleport System
        self.flash_users = set()
        self.user_positions = {}
        
        # Move to permanent position if it exists - run in background to retry until in room
        if "perm_pos" in self.settings:
            async def initial_move():
                while True:
                    try:
                        p = self.settings["perm_pos"]
                        await self.highrise.walk_to(Position(p["x"], p["y"], p["z"], p.get("facing", "FrontRight")))
                        print("Bot successfully moved to permanent position.")
                        break
                    except Exception as e:
                        if "not in room" in str(e).lower():
                            await asyncio.sleep(5)
                        else:
                            print(f"Initial move error: {e}")
                            break
            asyncio.create_task(initial_move())

        # Freeze System
        self.frozen_users = {} # {user_id: position_object}

        # Moderation System
        self.warnings = self.settings.get("warnings", {}) # {"username": [{"reporter": str, "reason": str, "time": str}]}
        self.dm_moderation = self.settings.get("dm_moderation", []) # List of admin usernames receiving DMs
        self.convo_cache = {} # {username.lower(): conversation_id} - Dynamic cache

        # Toggleable VIP Join Message
        self.vip_message_enabled = self.settings.get("vip_message_enabled", True)
        self.admin_message_enabled = self.settings.get("admin_message_enabled", True)
        self.owner_message_enabled = self.settings.get("owner_message_enabled", True)

        # Leveling System (data kept but system disabled)
        self.user_levels = self.settings.get("user_levels", {})
        
        # VIP Perks Broadcast
        asyncio.create_task(self.run_vip_perks_broadcast())
        asyncio.create_task(self.run_auto_invite())
        asyncio.create_task(self.run_vip_expiry_check())

        # Giveaway System
        self.giveaway_info = self.settings.get("giveaway_info", "")
        self.giveaway_entries = self.settings.get("giveaway_entries", [])

        # Dance Floor System
        self.dance_floor_area = {"min": None, "max": None}
        self.dance_floor_active = False 
        
        asyncio.create_task(self.run_user_dance_loop())





    async def notify_owners(self, message):
        """Send a DM to the bot owners logging an action."""
        try:
            # We need to find a conversation with the owner.
            # Since get_conversations might be paginated or slow, let's try a best effort.
            # But highrise.send_message needs a conversation_id.
            # If we don't know it, we might be stuck unless we have one cached.
            # However, usually bots have existing chats with owners.
            
            # Simple approach: Check active conversations
            conversations = []
            try:
                # Minimal fetch
                resp = await self.highrise.get_conversations() 
                conversations = resp.conversations
            except: pass

            for conversation in conversations:
                # Check members
                if hasattr(conversation, 'members'):
                    for member in conversation.members:
                        # Depending on SDK, member might be an object with username or just ID
                        # Usually it is Member object with user_id. We might need to resolve it.
                        # Wait, conversation.members usually has User objects?
                        # Let's try to assume we can match username or ID.
                        # Actually, fetching all conversations is expensive.
                        # Let's use a simpler heuristic: If we know the user ID of OWNER, use it?
                        # No, send_message needs convid.
                        
                        # Alternative: If owner is in the room, use whisper ?
                        # The user asked for "dm message".
                        # Let's just try to send it if we find the name in the member list (if user objects are populated).
                        pass
            
            # Since getting conversation ID is hard dynamically without extensive API calls, 
            # I will implement a "Whisper fallback" if owner is in room, 
            # and only try DM if I can find the conversation easily (e.g. they DM'd me recently).
            # ACTUALLY, I can iterate self.OWNERS and check if I have a cached conversation ID?
            
            # Let's iterate conversations and try to find the owner by name if possible.
            # This implementation assumes we can inspect conversation members.
            
            # For this task, I will add the logic but wrap it safely.
            # Better strategy: When OWNER DMs the bot, cache that conversation ID.
            pass
        except: pass

    async def report_moderation_action(self, action, target, moderator):
        """Report an action to the owner and interested admins via DM."""
        # Log to console
        print(f"MOD ACTION: {moderator} used {action} on {target}")
        
        # Determine who to notify
        mod_lower = moderator.lower()
        
        # Base notification list: Hardcoded Owners + Opted-in Admins
        # WE MUST include OWNERS to satisfy "owner ko dm notification nhi arha"
        hardcoded = [u.lower() for u in self.hardcoded_owners]
        opted_in = [u.lower() for u in self.dm_moderation]
        saved_owners = [u.lower() for u in self.settings.get("owners", [])]
        
        # Final list (unique, excluding the person who did the action)
        reporting_list = list(set(hardcoded + opted_in + saved_owners))
        reporting_list = [u for u in reporting_list if u != mod_lower]
        
        try:
            # Message content
            log_msg = (
                f"🚨 **MODERATION REPORT** 🚨\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👮 **Moderator:** @{moderator}\n"
                f"🔨 **Action:** {action}\n"
                f"🎯 **Target:** @{target}\n"
                f"🏠 **Room:** {self.current_room_name}\n"
                f"⏰ **Date/Time:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            
            for admin_name in reporting_list:
                try:
                    # Resolve ID via WebAPI to bypass conversation_id requirement
                    resp = await self.webapi.get_users(username=admin_name, limit=1)
                    if resp.users:
                        admin_id = resp.users[0].user_id
                        # Send DM directly to Inbox without needing conversation ID
                        await self.highrise.send_message_bulk([admin_id], log_msg)
                        await asyncio.sleep(0.5) 
                except Exception as e:
                    print(f"Failed to send mod log to {admin_name}: {e}")
        except Exception as e:
            print(f"Failed to report moderation action: {e}")

    def _tr(self, key):
        """Helper to get translated strings with English fallback."""
        return self.translations.get(self.language, self.translations["english"]).get(key, self.translations["english"].get(key, key))

    async def safe_chat(self, message):
        """Send chat messages in chunks to avoid Highrise character limits."""
        if not message or self.is_shutting_down: return
        try:
            # Highrise limit is around 256 for public chat
            max_len = 200 
            chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]
            for chunk in chunks:
                if self.is_shutting_down: break
                await self.highrise.chat(chunk)
                await asyncio.sleep(0.3) 
        except Exception as e:
            if "closing transport" in str(e).lower() or "connected" in str(e).lower():
                self.is_shutting_down = True
            else:
                print(f"Chat Error: {e}")



    async def run_emote_loop(self):
        """Background task to keep emotes playing non-stop, timed per emote duration."""
        # Track when each user's emote was last triggered: {user_id: (sent_time, emote_id)}
        last_sent: dict = {}
        MIN_WAIT = 2.5  # minimum seconds between retriggers for very short/looping emotes

        while not self.is_shutting_down:
            try:
                now = asyncio.get_event_loop().time()

                for user_id, emote_id in list(self.looping_users.items()):
                    if self.is_shutting_down:
                        break

                    prev_time, prev_emote = last_sent.get(user_id, (0, None))

                    # Get the actual emote duration; fall back to MIN_WAIT
                    duration = self.emote_duration_map.get(emote_id, 0)
                    wait = max(duration * 0.95, MIN_WAIT) if duration > 0 else MIN_WAIT

                    # Re-send if emote changed OR enough time has passed
                    if prev_emote != emote_id or (now - prev_time) >= wait:
                        try:
                            await self.highrise.send_emote(emote_id, user_id)
                            last_sent[user_id] = (asyncio.get_event_loop().time(), emote_id)
                        except Exception as e:
                            err = str(e).lower()
                            if "closing transport" in err:
                                self.is_shutting_down = True
                                break
                            if "not in room" in err:
                                await asyncio.sleep(3)
                                break
                            self.looping_users.pop(user_id, None)
                            last_sent.pop(user_id, None)

                # Clean up users no longer looping
                for uid in list(last_sent.keys()):
                    if uid not in self.looping_users:
                        del last_sent[uid]

                await asyncio.sleep(0.3)

            except Exception as e:
                if "closing transport" in str(e).lower():
                    self.is_shutting_down = True
                    break
                await asyncio.sleep(2)

    async def auto_broadcast(self):
        while True:
            await asyncio.sleep(self.broadcast_interval)
            try:
                if self.broadcast_message:
                    # Public broadcast
                    await self.highrise.chat(self.broadcast_message)
                    
                    # Private broadcast to online subscribers
                    room_users = (await self.highrise.get_room_users()).content
                    for target, _ in room_users:
                        if target.username in self.subscribers:
                            try:
                                await self.highrise.send_whisper(target.id, f"📢 [Broadcast]: <#FFFF00>{self.broadcast_message}")
                            except:
                                pass
            except Exception as e:
                print(f"Broadcast error: {e}")

    async def run_auto_tip(self):
        """Background task to tip active users automatically."""
        tip_ids = {"1": "gold_bar_1", "5": "gold_bar_5", "10": "gold_bar_10", "50": "gold_bar_50", "100": "gold_bar_100", "500": "gold_bar_500", "1000": "gold_bar_1000"}
        while not self.is_shutting_down:
            interval = getattr(self, "autotip_interval", 600)
            await asyncio.sleep(interval)
            
            if self.is_shutting_down: break
            
            if getattr(self, "auto_tip", False):
                try:
                    # Check Balance
                    wallet = (await self.highrise.get_wallet()).content
                    bot_gold = 0
                    for item in wallet:
                        if item.type == 'gold':
                            bot_gold = item.amount
                            break
                    if bot_gold <= 0:
                        await self.highrise.chat("bot have insufficient balance")
                        continue

                    amount = str(getattr(self, "autotip_amount", 1))
                    tip_id = tip_ids.get(amount, "gold_bar_1")
                    
                    room_users = (await self.highrise.get_room_users()).content
                    # Filter for real users (not bot)
                    real_users = [u for u, _ in room_users if u.id != self.bot_id]
                    if real_users and not self.is_shutting_down:
                        target = random.choice(real_users)
                        await self.highrise.tip_user(target.id, tip_id)
                        await self.highrise.chat(f"🎁 <#00FF00>Auto-Tip System selected @{target.username} for a {amount} Gold gift! 🎉")
                except Exception as e:
                    if "closing transport" in str(e).lower():
                        self.is_shutting_down = True
                        break
                    if "not in room" in str(e).lower():
                        await asyncio.sleep(5)
                    else:
                        print(f"Auto-tip error: {e}")

    async def run_loop_messages(self):
        """Background task to send loop messages periodically with independent timing."""
        last_sent = {} # {slot_num: timestamp}
        while True:
            try:
                # Optimized check: sleep 1s between checks
                await asyncio.sleep(1)
                
                if not getattr(self, "loop_running", False):
                    continue
                
                loop_messages = getattr(self, "loop_messages", {})
                loop_intervals = getattr(self, "loop_intervals", {})
                global_cooldown = getattr(self, "loop_cooldown", 60)
                
                now = time.time()
                for slot_num in sorted(loop_messages.keys()):
                    message = loop_messages[slot_num]
                    if not message or not message.strip():
                        continue
                        
                    # Determine interval for this slot
                    interval = loop_intervals.get(slot_num, global_cooldown)
                    last = last_sent.get(slot_num, 0)
                    
                    if now - last >= interval:
                        # Check if multiple bots are running - might want slightly different timings
                        # but for now we just send.
                        await self.highrise.chat(f"🔁 {message}")
                        last_sent[slot_num] = now
                        # Add a tiny delay between messages if multiple trigger at once to avoid spam kick
                        await asyncio.sleep(0.5) 
                        
            except Exception as e:
                if "not in room" in str(e).lower():
                    await asyncio.sleep(5)
                else:
                    print(f"Loop message error: {e}")

    async def run_vip_perks_broadcast(self):
        """Disabled VIP perks broadcast."""
        while True:
            await asyncio.sleep(3600)

    async def run_vip_expiry_check(self):
        """Periodically checks and removes expired VIP users from the system."""
        while not self.is_shutting_down:
            try:
                # Handle migration if it's still a list for some reason
                if isinstance(self.VIPS, list):
                    self.VIPS = {v: "permanent" for v in self.VIPS}
                
                if isinstance(self.VIPS, dict):
                    now = time.time()
                    expired_users = []
                    
                    for username, expiration in self.VIPS.items():
                        # Only check if it's a numeric timestamp (not "permanent")
                        if expiration != "permanent" and isinstance(expiration, (int, float)):
                            if now > expiration:
                                expired_users.append(username)
                    
                    if expired_users:
                        for username in expired_users:
                            self.VIPS.pop(username, None)
                            print(f"DEBUG: VIP status expired and removed for @{username}")
                        
                        # Save updated status
                        self.settings["vips"] = self.VIPS
                        self.save_settings()
                        
            except Exception as e:
                print(f"VIP expiry check error: {e}")
            
            # Check every 10 minutes (600 seconds)
            await asyncio.sleep(600)

    async def run_auto_invite(self):
        """Automatically send room invites to all recent conversations every 45 minutes."""
        # Wait 10 minutes before starting first automatic invite batch
        await asyncio.sleep(600)
        while not self.is_shutting_down:
            try:
                print("Starting automatic room invite cycle...")
                conversations_resp = await self.highrise.get_conversations()
                conversations = getattr(conversations_resp, 'conversations', [])
                
                if conversations:
                    inv_messages = [
                        "<#00FFFF>🥳 Dosto room join karo party chal rahi hai! Jaldi aao! 🎉 🔥",
                        "<#FF69B4>🎵 We are playing the best tracks right now! Join our room for non-stop music and amazing vibes! 🎶✨",
                        "<#FFD700>👑 Upgrade to VIP to unlock premium perks, special teleports, and exclusive areas! Type !buyvip now! 💎✨",
                        "<#00FF00>🎮 Calling all gamers! Drop into our room for some epic high-level gameplay and chill sessions! 🕹️🏆",
                        "<#FF8C00>🎸 Live DJ! Let's vibe to the beats together. Hop in and request your favorite songs! 🎵🎧",
                        "<#8A2BE2>⭐ Become a VIP today and get access to exclusive Emotes & Mod features! 🌟👑",
                        "<#FF4500>🎲 Game night is on! Join us for fun mini-games, logic puzzles, and great company! 🎯🃏"
                    ]
                    count = 0
                    for conv in conversations:
                        try:
                            # Skip if this is a system message or we already invited recently (optional, but let's keep it simple)
                            msg = random.choice(inv_messages)
                            await self.highrise.send_message(conv.id, msg)
                            await self.highrise.send_message(conv.id, "Join our room!", "invite", self.room_id)
                            count += 1
                            await asyncio.sleep(1.5) # Throttle to avoid rate limits
                        except:
                            continue
                    print(f"Auto-invite cycle complete. Sent {count} invites.")
                
                # 45 minutes = 2700 seconds
                await asyncio.sleep(2700)
                
            except Exception as e:
                print(f"Auto-invite loop error: {e}")
                await asyncio.sleep(300)

    async def run_follow_loop(self):
        """Continuously follows the target user."""
        while True:
            try:
                if self.following_user:
                    room_users = (await self.highrise.get_room_users()).content
                    target_pos = None
                    for user, pos in room_users:
                        if user.username.lower() == self.following_user.lower():
                            target_pos = pos
                            break
                    
                    if target_pos and isinstance(target_pos, Position):
                        # Simple follow: walk to position
                        # We might stomp on them, so maybe offset slightly?
                        # For now, exact position is fine or slightly offset
                        # Highrise walk_to needs Position object
                        await self.highrise.walk_to(target_pos)
            except Exception as e:
                if "not in room" in str(e).lower():
                    await asyncio.sleep(5)
                else:
                    print(f"Follow error: {e}")
            
            await asyncio.sleep(1) # Frequency of updates

    async def run_all_emotes_loop(self):
        """Loop only the floss dance continuously."""
        print("Starting floss dance loop...")
        while True:
            if not self.playing_all_emotes:
                await asyncio.sleep(2)
                continue
            try:
                await self.highrise.send_emote("dance-floss")
                await asyncio.sleep(8)
            except Exception as e:
                if "not in room" in str(e).lower():
                    await asyncio.sleep(5)
                else:
                    print(f"Floss dance loop error: {e}")
                    await asyncio.sleep(3)

    async def run_user_dance_loop(self):
        """Make users dance if they are on the dance floor."""
        # Track each user's current emote index for cycling
        user_emote_indices = {}
        
        while True:
            try:
                if getattr(self, "dance_floor_active", False):
                    try:
                        room_users = (await self.highrise.get_room_users()).content
                    except Exception as e:
                        if "not in room" in str(e).lower():
                            await asyncio.sleep(5)
                            continue
                        raise e
                    
                    # Prepare list of emotes to cycle/randomize
                    # Use ALL emotes from emotes.json (one by one)
                    if not hasattr(self, "emote_list") or not self.emote_list:
                         # Emotes not loaded yet
                         await asyncio.sleep(5)
                         continue
                         
                    dance_emotes = self.emote_list.copy()
                    
                    if not dance_emotes:
                        await asyncio.sleep(5)
                        continue

                    area = getattr(self, "dance_floor_area", {})
                    has_area = area.get("min") is not None and area.get("max") is not None

                    # Track users currently in the dance floor
                    users_in_area = []
                    
                    for user, pos in room_users:
                        if user.id == self.bot_id: continue # Skip bot

                        # Check if user is in defined area (if defined)
                        in_area = True
                        if has_area:
                            if isinstance(pos, Position):
                                min_x = min(area["min"].x, area["max"].x)
                                max_x = max(area["min"].x, area["max"].x)
                                min_y = min(area["min"].y, area["max"].y)
                                max_y = max(area["min"].y, area["max"].y)
                                min_z = min(area["min"].z, area["max"].z)
                                max_z = max(area["min"].z, area["max"].z)

                                if not (min_x <= pos.x <= max_x and 
                                        min_y <= pos.y <= max_y and 
                                        min_z <= pos.z <= max_z):
                                    in_area = False
                            else:
                                in_area = False # Anchor position usually considered separate or needing logic
                        
                        if in_area:
                            users_in_area.append(user.id)
                            
                            # Initialize user's emote index if not exists
                            if user.id not in user_emote_indices:
                                user_emote_indices[user.id] = {
                                    "index": 0,
                                    "emote_list": dance_emotes.copy()
                                }
                                # Shuffle the list for this user for variety
                                random.shuffle(user_emote_indices[user.id]["emote_list"])
                            
                            # Get user's current emote
                            user_data = user_emote_indices[user.id]
                            current_index = user_data["index"]
                            user_emote_list = user_data["emote_list"]
                            
                            # Get the emote name and ID
                            emote_name = user_emote_list[current_index]
                            emote_id = self.emotes.get(emote_name)
                            
                            if emote_id:
                                try:
                                    await self.highrise.send_emote(emote_id, user.id)
                                    # Tiny delay to avoid rate limit spikes
                                    await asyncio.sleep(0.1)
                                except Exception as e:
                                    pass
                            
                            # Move to next emote for this user
                            user_data["index"] = (current_index + 1) % len(user_emote_list)
                            
                            # If we've cycled through all emotes, shuffle for next round
                            if user_data["index"] == 0:
                                random.shuffle(user_data["emote_list"])
                    
                    # Clean up tracking for users who left the dance floor
                    users_to_remove = [uid for uid in user_emote_indices.keys() if uid not in users_in_area]
                    for uid in users_to_remove:
                        del user_emote_indices[uid]
                    
                    # Wait for emote duration before next cycle
                    await asyncio.sleep(15)
                else:
                    # Clear tracking when dance floor is inactive
                    user_emote_indices.clear()
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"Dance loop error: {e}")
                await asyncio.sleep(5)

    async def on_whisper(self, user, message):
        print(f"{user.username} whispered: {message}")
        msg = message.lower().strip()
        if msg == "!sub":
            return
        elif msg == "!unsub":
            # Remove from list
            removed = False
            self.subscribers = [s for s in self.subscribers if not (
                (isinstance(s, dict) and s.get("id") == user.id) or 
                (isinstance(s, str) and s.lower() == user.username.lower())
            )]
            
            if len(self.settings.get("subscribers", [])) != len(self.subscribers):
                self.settings["subscribers"] = self.subscribers
                self.save_settings()
                await self.highrise.send_whisper(user.id, "🔒 <#FF0000>You have unsubscribed. Bot commands, emotes, and teleports are now LOCKED! ❌")
            else:
                await self.highrise.send_whisper(user.id, "⚠️ <#FFFF00>You are not subscribed.")

    async def on_message(self, user_id, conversation_id, is_new_conversation):
        """Handle incoming DM messages (Profile DMs)."""
        # Ignore messages from the bot itself to prevent infinite loops
        if user_id == self.bot_id:
            return
            
        print(f"DEBUG: New DM from {user_id} in conversation {conversation_id}")
        try:
            # Get the message content
            response = await self.highrise.get_messages(conversation_id)
            if hasattr(response, 'messages') and response.messages:
                last_msg_obj = response.messages[0]
                msg_text = last_msg_obj.content.lower().strip()
                print(f"DEBUG: DM Content: {msg_text}")
                
                # Get username (check room first, then Web API)
                username = None
                room_users = (await self.highrise.get_room_users()).content
                for u, _ in room_users:
                    if u.id == user_id:
                        username = u.username
                        break
                
                if not username:
                    try:
                        user_resp = await self.webapi.get_user(user_id)
                        username = user_resp.user.username
                    except Exception as e:
                        print(f"WebAPI error for {user_id}: {e}")
                        
                if not username:
                    print(f"Could not resolve username for {user_id}")
                    return

                # Update Convo Cache
                self.convo_cache[username.lower()] = conversation_id

                # Check Blocked Users
                if username in self.blocked_users:
                    print(f"Blocked user {username} tried to DM bot.")
                    return

                if msg_text == "!sub":
                    # Check sub status
                    is_subbed = False
                    for s in self.subscribers:
                        if isinstance(s, dict) and s.get("id") == user_id:
                            is_subbed = True
                            break
                        elif isinstance(s, str) and s.lower() == username.lower():
                            is_subbed = True
                            break
                            
                    if not is_subbed:
                        self.subscribers.append({"id": user_id, "username": username})
                        self.settings["subscribers"] = self.subscribers
                        self.save_settings()
                        reply = f"🔓 Success! @{username}, I have unlocked all my commands, emotes, and teleports for you! ✅\n\nCome to the room and have fun! 🤖"
                    else:
                        reply = f"⚠️ @{username}, you are already subscribed and your commands are unlocked!"
                    await self.highrise.send_message(conversation_id, reply)
                    
                elif msg_text == "!unsub":
                    initial_len = len(self.subscribers)
                    self.subscribers = [s for s in self.subscribers if not (
                        (isinstance(s, dict) and s.get("id") == user_id) or 
                        (isinstance(s, str) and s.lower() == username.lower())
                    )]
                    
                    if len(self.subscribers) < initial_len:
                        self.settings["subscribers"] = self.subscribers
                        self.save_settings()
                        reply = f"🔒 @{username}, you have unsubscribed. My commands and teleports are now LOCKED for you. ❌"
                    else:
                        reply = f"⚠️ @{username}, you are not currently subscribed."
                    await self.highrise.send_message(conversation_id, reply)
                elif msg_text == "!emotelist":
                    if not self.emotes:
                        await self.highrise.send_message(conversation_id, "❌ No emotes configured.")
                    else:
                        await self.highrise.send_message(conversation_id, f"🎭 Sending {len(self.emote_list)} emotes...")
                        # Create numbered list in chunks
                        chunk_size = 20
                        for i in range(0, len(self.emote_list), chunk_size):
                            chunk = self.emote_list[i : i + chunk_size]
                            lines = []
                            for j, name in enumerate(chunk, i + 1):
                                lines.append(f"{j}. {name}")
                            
                            full_chunk = "\n".join(lines)
                            await self.highrise.send_message(conversation_id, f"🎭 Emotes {i+1}-{i+len(chunk)}:\n{full_chunk}")
                            await asyncio.sleep(0.5)

                elif msg_text.startswith("!help"):
                    parts = msg_text.split()
                    category = parts[1].lower() if len(parts) > 1 else ""

                    if not category:
                        # Main Menu
                        help_intro = (
                            "<#FF00FF>🤖 PREMIUM BOT COMMAND CATALOG 🤖\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>✨ Type <#FFFFFF>!help [category] <#FFFF00>for details:\n"
                            "<#00FFFF>🔹 !help public <#800080>- General commands\n"
                            "<#00FFFF>🔹 !help emotelist <#800080>- Show all Emotes\n"
                            "<#00FFFF>🔹 !help fun <#800080>- Fun & Games\n"
                            "<#00FFFF>🔹 !help vip <#800080>- VIP Features\n"
                            "<#00FFFF>🔹 !help teleports <#800080>- Teleport list\n"
                            "<#00FFFF>🔹 !help giveaway <#800080>- Giveaway info\n"
                            "<#00FFFF>🔹 !help reaction <#800080>- Rxn commands\n"
                            "<#00FFFF>🔹 !help botoutfit <#800080>- Bot Outfit Mgmt\n"
                            "<#00FFFF>🔹 !help moderator <#800080>- Admin tools\n"
                            "<#00FFFF>🔹 !help owner <#800080>- Owner controls\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FF00FF>🔗 Type !sub to unlock everything!"
                        )
                        await self.highrise.send_message(conversation_id, help_intro)
                    
                    elif category == "emotelist":
                        if not self.emotes:
                            await self.highrise.send_message(conversation_id, "❌ No emotes configured.")
                        else:
                            await self.highrise.send_message(conversation_id, f"🎭 Sending {len(self.emote_list)} emotes...")
                            chunk_size = 20
                            for i in range(0, len(self.emote_list), chunk_size):
                                chunk = self.emote_list[i : i + chunk_size]
                                lines = []
                                for j, name in enumerate(chunk, i + 1):
                                    lines.append(f"{j}. {name}")
                                
                                full_chunk = "\n".join(lines)
                                await self.highrise.send_message(conversation_id, f"🎭 Emotes {i+1}-{i+len(chunk)}:\n{full_chunk}")
                                await asyncio.sleep(0.5)

                    elif category == "public":
                        msg = (
                            "<#FF00FF>🌟 PUBLIC COMMANDS 🌟\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!sub / !unsub <#800080>- Lock/Unlock Bot\n"
                            "<#FFFF00>!ping <#800080>- Status\n"
                            "<#FFFF00>!user <#800080>- Who is in room?\n"
                            "<#FFFF00>!profile <#800080>- Your stats\n"
                            "<#FFFF00>!emotelist <#800080>- Emote Catalog\n"
                            "<#FFFF00>!level / !xp <#800080>- Check Level\n"
                            "<#FFFF00>!lb / !lb2 <#800080>- Leaderboards\n"
                            "<#FFFF00>!flash [on/off] <#800080>- Instant TP Mode\n"
                            "<#FFFF00>!info <#800080>- Bot Info"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category == "fun":
                        msg = (
                            "<#FF00FF>🎭 FUN COMMANDS 🎭\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!rizz <#800080>- Pickup lines\n"
                            "<#FFFF00>!roast @user <#800080>- Roast response\n"
                            "<#FFFF00>!flirt <#800080>- Flirty message\n"
                            "<#FFFF00>!joke <#800080>- Tell a joke\n"
                            "<#FFFF00>!shayari <#800080>- Poetry\n"
                            "<#FFFF00>!love @user <#800080>- Love %\n"
                            "<#FFFF00>!hate @user <#800080>- Hate %\n"
                            "<#FFFF00>!deathyear @user <#800080>- Prediction"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category == "vip":
                        msg = (
                            "<#FF00FF>👑 VIP COMMANDS 👑\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!vipcost <#800080>- Price Check\n"
                            "<#FFFF00>!viplist <#800080>- Member List\n"
                            "<#FFFF00>!buyvip <#800080>- Buy VIP (Tip first)"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category in ["teleports", "tele"]:
                        msg = (
                            "<#FF00FF>📍 TELEPORT COMMANDS 📍\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!telelist <#800080>- List all Locations\n"
                            "<#FFFF00>!create tele [name] <#800080>- (Admin) Set Loc\n"
                            "<#FFFF00>!remtele [name] <#800080>- (Admin) Del Loc\n"
                            "<#FFFF00>!cleartele <#800080>- (Admin) Del All\n"
                            "<#FFFF00>[name] <#800080>- Type name to flash!"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category == "giveaway":
                        msg = (
                            "<#FF00FF>🎁 GIVEAWAY COMMANDS 🎁\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!giveaway <#800080>- Show Status\n"
                            "<#FFFF00>!giveaway enter <#800080>- Join In\n"
                            "<#FFFF00>!giveaway info [msg] <#800080>- (Admin) Set\n"
                            "<#FFFF00>!giveaway winner <#800080>- (Admin) Pick\n"
                            "<#FFFF00>!giveaway reset <#800080>- (Admin) Clear"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category == "reaction":
                        msg = (
                            "<#FF00FF>❤️ REACTION COMMANDS ❤️\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!heartall <#800080>- ❤️ Everyone\n"
                            "<#FFFF00>!winkall <#800080>- 😉 Everyone\n"
                            "<#FFFF00>!thumbsall <#800080>- 👍 Everyone\n"
                            "<#FFFF00>!waveall <#800080>- 👋 Everyone\n"
                            "<#FFFF00>!clapall <#800080>- 👏 Everyone\n"
                            "<#888888>(Moderator Only)"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category in ["moderator", "mod"]:
                        msg = (
                            "<#FF00FF>🛡️ MODERATOR COMMANDS 🛡️\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!kick @user <#800080>- Kick\n"
                            "<#FFFF00>!ban @user [m] <#800080>- Ban\n"
                            "<#FFFF00>!unban @user <#800080>- Unban\n"
                            "<#FFFF00>!mute @user [m] <#800080>- Mute\n"
                            "<#FFFF00>!unmute @user <#800080>- Unmute\n"
                            "<#FFFF00>!freeze / !unfreeze <#800080>- Freeze Move\n"
                            "<#FFFF00>!summon @user/all <#800080>- Teleport Here\n"
                            "<#FFFF00>!come <#800080>- Bot comes to you\n"
                            "<#FFFF00>!tip @user [amt] <#800080>- Gift Gold\n"
                            "<#FFFF00>!tipall [amt] <#800080>- Gift Everyone\n"
                            "<#FFFF00>!invite [msg] <#800080>- Mass DM Invite\n"
                            "<#FFFF00>!history <#800080>- Tip History"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category == "botoutfit":
                        msg = (
                            "<#FF00FF>👕 BOT OUTFIT COMMANDS 👕\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!savefit [name] <#800080>- Save current outfit\n"
                            "<#FFFF00>!loadfit [name] <#800080>- Load saved outfit\n"
                            "<#FFFF00>!fitlist <#800080>- List all saved outfits\n"
                            "<#FFFF00>!removefit [name] <#800080>- Delete saved outfit\n"
                            "<#888888>(Owner Only)"
                        )
                        await self.highrise.send_message(conversation_id, msg)

                    elif category == "owner":
                        msg = (
                            "<#FF00FF>⚡ OWNER COMMANDS ⚡\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!addadmin @user\n"
                            "<#FFFF00>!remadmin @user\n"
                            "<#FFFF00>!owner @user\n"
                            "<#FFFF00>!remowner @user\n"
                            "<#FFFF00>!block @user <#800080>- Block Mod\n"
                            "<#FFFF00>!cashout <#800080>- Withdraw Gold\n"
                            "<#FFFF00>!restartbot <#800080>- Reboot\n"
                            "<#FFFF00>!clear data <#800080>- WIPE ALL\n"
                            "<#FFFF00>!uptime <#800080>- Bot Status\n"
                            "<#FFFF00>!autotip [on/off/...] <#800080>- Auto Tipping"
                        )
                        await self.highrise.send_message(conversation_id, msg)
                    
                    else:
                         await self.highrise.send_message(conversation_id, "❌ Unknown category. Type !help for the menu.")

                elif "giveaway" in msg_text:
                    if not self.giveaway_info:
                        reply = "🔕 No active giveaway at the moment. Stay tuned!"
                    else:
                        count = len(self.giveaway_entries)
                        reply = (
                            f"🎁 CURRENT GIVEAWAY 🎁\n"
                            f"{self.giveaway_info}\n"
                            f"------------------\n"
                            f"Entries: {count}\n"
                            f"Type !giveaway enter in the room to join!"
                        )
                    await self.highrise.send_message(conversation_id, reply)

                # DM Moderation Commands (Admins Only)
                # DM Moderation Commands (Admins Only)
                elif msg_text.startswith("!") and username in self.ADMINS:
                    parts = msg_text.split() # Split by whitespace
                    cmd = parts[0]
                    
                    if cmd in ["!kick", "!ban", "!mute", "!unmute", "!freeze", "!unfreeze"]:
                        if len(parts) < 2:
                            await self.highrise.send_message(conversation_id, f"⚠️ Usage: {cmd} @username [args]")
                        else:
                            target_name = parts[1].replace("@", "").lower()
                            
                            target_user = None
                            target_pos = None
                            
                            # Fetch users once
                            try:
                                room_users = (await self.highrise.get_room_users()).content
                                for u, pos in room_users:
                                    if u.username.lower() == target_name:
                                        target_user = u
                                        target_pos = pos
                                        break
                            except Exception as e:
                                await self.highrise.send_message(conversation_id, f"❌ Error fetching room users: {e}")
                                return
                            
                            if not target_user:
                                # Try web api for name resolution if just needed for ban/kick but they need to be in room for moderate_room usually?
                                # moderate_room needs User ID. If not in room, we can't get ID easily unless cached.
                                await self.highrise.send_message(conversation_id, f"❌ User @{target_name} not found in the room.")
                            else:
                                try:
                                    if cmd == "!kick":
                                        await self.highrise.moderate_room(target_user.id, "kick")
                                        await self.highrise.send_message(conversation_id, f"✅ @{target_name} has been KICKED.")
                                        await self.report_moderation_action("Kick", target_name, username)
                                        
                                    elif cmd == "!ban":
                                        duration = 300 
                                        if len(parts) > 2 and parts[2].isdigit():
                                            duration = int(parts[2]) * 60
                                        await self.highrise.moderate_room(target_user.id, "ban", duration)
                                        await self.highrise.send_message(conversation_id, f"✅ @{target_name} has been BANNED for {duration//60} mins.")
                                        await self.report_moderation_action(f"Ban ({duration//60}m)", target_name, username)
                                        
                                    elif cmd == "!mute":
                                        duration = 300 
                                        if len(parts) > 2 and parts[2].isdigit():
                                            duration = int(parts[2]) * 60
                                        await self.highrise.moderate_room(target_user.id, "mute", duration)
                                        await self.highrise.send_message(conversation_id, f"✅ @{target_name} has been MUTED for {duration//60} mins.")
                                        await self.report_moderation_action(f"Mute ({duration//60}m)", target_name, username)
                                        
                                    elif cmd == "!unmute":
                                        # Using 1 sec mute to unmute
                                        await self.highrise.moderate_room(target_user.id, "mute", 1)
                                        await self.highrise.send_message(conversation_id, f"✅ @{target_name} has been UNMUTED.")
                                        await self.report_moderation_action("Unmute", target_name, username)
                                    elif cmd == "!freeze":
                                        await self.highrise.moderate_room(target_user.id, "mute", 86400)
                                        self.frozen_users[target_user.id] = target_pos
                                        await self.highrise.send_message(conversation_id, f"✅ @{target_name} has been FROZEN.")
                                        await self.highrise.chat(f"🥶 @{target_user.username} has been FROZEN!")
                                        await self.report_moderation_action("Freeze", target_name, username)
                                        
                                    elif cmd == "!unfreeze":
                                        if target_user.id in self.frozen_users:
                                            del self.frozen_users[target_user.id]
                                        await self.highrise.moderate_room(target_user.id, "mute", 1)
                                        await self.highrise.send_message(conversation_id, f"✅ @{target_name} has been UNFROZEN.")
                                        await self.highrise.chat(f"🔥 @{target_user.username} has been UNFROZEN!")
                                        await self.report_moderation_action("Unfreeze", target_name, username)
                                    
                                except Exception as ex:
                                    print(f"Mod Action Error: {ex}")
                                    await self.highrise.send_message(conversation_id, f"❌ Error executing {cmd}: {ex}")

                elif msg_text in ["!emotelist", "emotelist"]:
                    if not self.emotes:
                        await self.highrise.send_message(conversation_id, "❌ No emotes configured.")
                    else:
                        header = f"🎭 BOT EMOTE LIST ({len(self.emote_list)} emotes) 🎭\nTo use: Type the number or name in the room!"
                        await self.highrise.send_message(conversation_id, header)
                        
                        # Create numbered list in chunks
                        chunk_size = 20
                        for i in range(0, len(self.emote_list), chunk_size):
                            chunk = self.emote_list[i : i + chunk_size]
                            lines = []
                            for j, name in enumerate(chunk, i + 1):
                                lines.append(f"{j}. {name}")
                            
                            full_chunk = "\n".join(lines)
                            await self.highrise.send_message(conversation_id, f"🎭 Emotes {i+1}-{i+len(chunk)}:\n{full_chunk}")
                            await asyncio.sleep(0.5)

                elif msg_text.startswith("!profile"):
                    try:
                        # Handle potential target
                        target_username = username
                        target_id = user_id
                        
                        parts = msg_text.split()
                        if len(parts) > 1:
                            target_username = parts[1].replace("@", "")
                            # Try to resolve ID via WebAPI
                            resp = await self.webapi.get_users(username=target_username, limit=1)
                            if resp.users:
                                target_id = resp.users[0].user_id
                            else:
                                await self.highrise.send_message(conversation_id, f"❌ User @{target_username} not found.")
                                return

                        # Data resolution
                        stats = self.user_stats.get(target_username, {})
                        joined_date = stats.get("joined_bot_date", stats.get("first_seen", "Not Subscribed ❌"))
                        
                        # Check online status for accurate last seen
                        r_users = (await self.highrise.get_room_users()).content
                        is_online = any(u.username.lower() == target_username.lower() for u, _ in r_users)
                        last_seen = stats.get("last_seen", "Online Now 🟢") if not is_online else "Online Now 🟢"
                        
                        is_banned = "Yes 🚫" if target_username.lower() in [b.lower() for b in self.banned_users] else "No ✅"
                        
                        # VIP Info
                        is_vip_active = False
                        expiry_str = "N/A"
                        if isinstance(self.VIPS, dict) and target_username in self.VIPS:
                            exp = self.VIPS[target_username]
                            if exp == "permanent":
                                is_vip_active = True
                                expiry_str = "Permanent ♾️"
                            elif isinstance(exp, (int, float)):
                                if exp > time.time():
                                    is_vip_active = True
                                    date_obj = datetime.fromtimestamp(exp)
                                    expiry_str = date_obj.strftime("%Y-%m-%d")
                                else:
                                    expiry_str = "Expired"
                        
                        vip_history = self.settings.get("vip_history", [])
                        is_vip_status = "Yes ✅" if is_vip_active else "No ❌"
                        is_activated = "Active 🟢" if is_vip_active else "Inactive 🔴"
                        
                        # Rank & Activity
                        cmds_used = self.command_usage.get(target_username, 0)
                        msg_count = self.chat_stats.get(target_username, 0)
                        total_secs = self.user_times.get(target_username, 0)
                        
                        # Add current session time if online (we can check if they are in join_times but in DM we don't know for sure if online in room)
                        # But we can assume they might be.
                        hrs = total_secs // 3600
                        mins = (total_secs % 3600) // 60
                        score_pts = (total_secs // 60) + (msg_count * 2)
                        activity_score = f"{score_pts} Points 🌟 ({hrs}h {mins}m | {msg_count} msgs)"
                        
                        rank = "🌱 Newcomer"
                        if total_secs > 36000: rank = "🏆 Legend"
                        elif total_secs > 18000: rank = "🎖️ Veteran"
                        elif total_secs > 3600: rank = "👤 Regular"
                        elif cmds_used > 50: rank = "☘️ Active"
                        
                        # Format Profile Card
                        profile_card = (
                            f"👤 Profile of @{target_username}\n"
                            f"--------------------------------\n"
                            f"🗓️ Joined Date: {joined_date}\n"
                            f"🌐 Language: {self.language.title()}\n"
                            f"🛡️ Banned: {is_banned}\n"
                            f"🕒 Last Seen: {last_seen}\n\n"
                            f"🌟 VIP Status\n"
                            f"  - Is VIP: {is_vip_status}\n"
                            f"  - Activated: {is_activated}\n"
                            f"  - Expiry: {expiry_str}\n\n"
                            f"🏆 Rank Info\n"
                            f"  - Rank: {rank}\n"
                            f"  - Commands Used: {cmds_used}\n"
                            f"  - Activity Score: {activity_score}\n"
                            f"--------------------------------"
                        )
                        await self.highrise.send_message(conversation_id, profile_card)
                    except Exception as e:
                        print(f"Profile error in DM: {e}")
                        await self.highrise.send_message(conversation_id, "❌ Error fetching profile card.")

                elif msg_text == "!joke":
                    jokes_pool = [
                        "Santa: Oye, tu kyun ro raha hai?\nBanta: Yaar, meri 1 kilo ki baraf kho gayi hai.\nSanta: Oye, fikr mat kar, baraf hi toh hai, agli baar 2 kilo le aana!",
                        "Pappa: Beta, tere result ka kya hua?\nBeta: Headmaster ka beta fail ho gaya.\nPappa: Aur tu?\nBeta: Doctor ka beta bhi fail ho gaya.\n"
                        "Sharabi: Bottle mein se bhoot nikla aur bola, 'Kya hukum hai mere aaka?'\nSharabi: Ek chamcha dahi la de, mujhe kachumar banana hai!",
                        "Biwi: Aaj khane mein kya banaya hai?\nPati: Gussa!\nBiwi: Toh khud hi kha lo, mera toh pet bhara hai!"
                    ]
                    await self.highrise.send_message(conversation_id, f"😂 {random.choice(jokes_pool)}")
                elif msg_text == "!shayari":
                    shayari_pool = [
                        "Har phool ki ajab kahani hai,\nChup rehna bhi pyar ki nishani hai,\nKahin koi zakhm nahi phir bhi kyu dard ka ehsas hai,\nLagta hai dil ka ek tukda aaj bhi unke paas hai.",
                        "Zindagi jine ka sahara chahiye,\nDil ko ek tera nazara chahiye,\nKhushiyan mile na mile duniya mein,\nGar mile tum toh ek jahan chahiye.",
                        "Aankhon mein raha dil mein utar kar nahi dekha,\nKashti ke musafir ne samandar nahi dekha,\nBewaqt agar jaunga toh sab chowk padenge,\nEk umr hui main ne apna ghar nahi dekha."
                    ]
                    await self.highrise.send_message(conversation_id, f"💖 {random.choice(shayari_pool)}")

                elif msg_text == "!room":
                    room_link = f"🏠 *{self.room_name}* 🇪🇬\nادخل معانا يلا بينا!\n👉 https://high.rs/room?id=694642f094977936f78a313f&invite_id=6958a4f4cdac317262837bcf"
                    await self.highrise.send_message(conversation_id, room_link)

                elif msg_text == "!history":
                    if username in self.ADMINS or username in self.OWNERS:
                        if not self.tip_history:
                            await self.highrise.send_message(conversation_id, "📜 No tip history found yet.")
                        else:
                            reply = "📜 *RECENT 10 TIPS HISTORY* 📜\n"
                            # Take last 10 record newest first
                            recent_10 = self.tip_history[-10:]
                            recent_10.reverse()
                            for i, entry in enumerate(recent_10, 1):
                                reply += f"{i}. @{entry['username']} tipped {entry['amount']}G\n"
                            await self.highrise.send_message(conversation_id, reply)
                    else:
                        await self.highrise.send_message(conversation_id, "❌ Admin and Owner only!")

                elif msg_text == "!lb":
                    sorted_lb = sorted(self.user_times.items(), key=lambda x: x[1], reverse=True)[:10]
                    if not sorted_lb:
                        reply = "⏳ Time leaderboard is empty!"
                    else:
                        reply = "⏳ *TOP 10 TIME LEADERS* ⏳\n"
                        for i, (name, secs) in enumerate(sorted_lb, 1):
                            hrs = secs // 3600
                            mins = (secs % 3600) // 60
                            reply += f"{i}. @{name} - {hrs}h {mins}m\n"
                    await self.highrise.send_message(conversation_id, reply)

                elif msg_text == "!lb2":
                    sorted_chat = sorted(self.chat_stats.items(), key=lambda x: x[1], reverse=True)[:10]
                    if not sorted_chat:
                        reply = "💬 Chat leaderboard is empty!"
                    else:
                        reply = "💬 *TOP 10 CHAT LEADERS* 💬\n"
                        for i, (name, count) in enumerate(sorted_chat, 1):
                            reply += f"{i}. @{name} - {count} msgs\n"
                    await self.highrise.send_message(conversation_id, reply)

                elif msg_text == "!mytime":
                    secs = self.user_times.get(username, 0)
                    hrs = secs // 3600
                    mins = (secs % 3600) // 60
                    await self.highrise.send_message(conversation_id, f"🕒 @{username}, your total tracked time: {hrs}h {mins}m")

                elif msg_text.startswith("!time "):
                    try:
                        target = msg_text.split(" ")[1].replace("@", "").strip()
                        secs = self.user_times.get(target, 0)
                        hrs = secs // 3600
                        mins = (secs % 3600) // 60
                        await self.highrise.send_message(conversation_id, f"🕒 @{target} has spent {hrs}h {mins}m in the room.")
                    except:
                        await self.highrise.send_message(conversation_id, "Usage: !time @username")

                elif msg_text == "!cleartime":
                    if username in self.ADMINS:
                        self.user_times = {}
                        self.settings["user_times"] = {}
                        self.save_settings()
                        await self.highrise.send_message(conversation_id, "🗑️ Time leaderboard cleared!")
                    else:
                        await self.highrise.send_message(conversation_id, "❌ Admin only!")

                elif msg_text.startswith("!flash"):
                    parts = msg_text.split()
                    if len(parts) > 1:
                        target_arg = parts[1].lower()
                        if target_arg == "off":
                            if username in self.flash_users:
                                self.flash_users.remove(username)
                                if "flash_users" in self.settings and username in self.settings["flash_users"]:
                                    self.settings["flash_users"].remove(username)
                                    self.save_settings()
                                await self.highrise.send_message(conversation_id, "🚫 Flash Mode DISABLED. You will no longer teleport on click.")
                            else:
                                await self.highrise.send_message(conversation_id, "⚠️ Flash Mode is already disabled.")
                            return
                    
                    self.flash_users.add(username)
                    if "flash_users" not in self.settings: self.settings["flash_users"] = []
                    if username not in self.settings["flash_users"]:
                        self.settings["flash_users"].append(username)
                        self.save_settings()
                    await self.highrise.send_message(conversation_id, "⚡ Flash Mode ENABLED! You can now teleport by clicking anywhere in the room. (Type !flash off to disable) ⚡")

                elif msg_text == "!clearchat":
                    if username in self.ADMINS:
                        self.chat_stats = {}
                        self.settings["chat_stats"] = {}
                        self.save_settings()
                        await self.highrise.send_message(conversation_id, "🗑️ Chat leaderboard cleared!")
                    else:
                        await self.highrise.send_message(conversation_id, "❌ Admin only!")

                elif msg_text in ["!menu", "!help", "!setting"]:
                    trans = self.translations.get(self.language, self.translations["english"])
                    # Send educational guide first
                    how_to = (
                        "📖 *HOW TO USE THE BOT GUIDE* 📖\n\n"
                        "1️⃣ *Unlocking Commands*: Most commands are locked for new users. Type *!sub* to unlock everything instantly!\n\n"
                        "2️⃣ *Using Emotes*: You can use emotes by typing their name (e.g., *!wave*) or their number (e.g., *!1*). Type *!emotelist* to see all numbers.\n\n"
                        "3️⃣ *Teleporting*: Just type the name of a teleport (e.g., *f1*) in the room chat to move instantly.\n\n"
                        "4️⃣ *Fun Commands*: Try *!rizz*, *!roast*, or *!love @username* for some fun interactions!\n\n"
                        "5️⃣ *Non-Stop Emotes*: Any emote you start will loop forever until you type *!stop*.\n\n"
                        "--------------------------------"
                    )
                    await self.highrise.send_message(conversation_id, how_to)
                    await asyncio.sleep(1)

                    # Send menu components one by one
                    m_pub = trans.get("m_pub", self.translations["english"]["m_pub"])
                    m_fun = trans.get("m_fun", self.translations["english"]["m_fun"])
                    m_vip = trans.get("m_vip", self.translations["english"]["m_vip"])
                    m_mod = trans.get("m_mod", self.translations["english"]["m_mod"])
                    
                    header = "📋 *AVAILABLE COMMANDS LIST* 📋\n"
                    m_tele = trans.get("m_tele", "")
                    sections = [header, m_pub, m_fun, m_vip, m_tele, m_mod]
                    for sec in sections:
                        if sec.strip():
                            await self.highrise.send_message(conversation_id, sec)
                            await asyncio.sleep(0.5)
                
                else:
                    # Generic auto-reply for non-commands
                    reply = (
                        f"👋 هاي @{username}! 🤖 انا بوت {self.room_name} 🇪🇬\n"
                        "بوت مصنوع بواسطة to_xic_ 💪\n\nعشان تشوف الأوامر كلها ابعتلي:\n"
                        "👉 **!help**\n\n"
                        "عشان تشوف ستاتس حسابك ابعتلي:\n"
                        "👉 **!profile**\n\n"
                        "عشان تفتح الإيموتس والتيليبورتس ابعتلي:\n"
                        "👉 **!sub**\n\n"
                        "✨ انضم لينا هنا: https://high.rs/room?id=694642f094977936f78a313f&invite_id=6958a4f4cdac317262837bcf ✨"
                    )
                    await self.highrise.send_message(conversation_id, reply)

        except Exception as e:
            print(f"Error handling DM: {e}")



    def load_settings(self):
        if os.path.exists(self.settings_file):
            with open(self.settings_file, "r") as f:
                self.settings = json.load(f)
        else:
            self.settings = {}

    async def run_periodic_save(self):
        """Background task to save settings periodically if changed."""
        while True:
            await asyncio.sleep(10)
            if self.settings_dirty:
                self._save_to_disk()


    def _save_to_disk(self):
        """Actual synchronous file write."""
        try:
            with open(self.settings_file, "w") as f:
                json.dump(self.settings, f)
            self.settings_dirty = False
            # print("Settings saved to disk.")
        except Exception as e:
            print(f"Error saving settings: {e}")

    def save_settings(self):
        """Mark settings as needing save. Non-blocking."""
        self.settings_dirty = True

    async def _scan_room_moderators(self):
        """Scan users already in the room on bot start and detect moderators/designers."""
        await asyncio.sleep(3)  # Give the bot a moment to fully initialize
        try:
            resp = await self.highrise.get_room_users()
            users_in_room = resp.content
            print(f"[PRIV] Scanning {len(users_in_room)} users in room for moderator privileges...")
            for user, _ in users_in_room:
                try:
                    priv_resp = await self.highrise.get_room_privilege(user.id)
                    perms = priv_resp.content
                    if perms.moderator or perms.designer:
                        self.room_moderators.add(user.username.lower())
                        if user.username not in self.ADMINS:
                            self.ADMINS.append(user.username)
                        print(f"[PRIV] ✅ {user.username} granted auto-admin (moderator/designer)")
                    else:
                        print(f"[PRIV] {user.username} → no mod/designer privilege")
                except Exception as _e:
                    print(f"[PRIV] ⚠️ Could not check privilege for {user.username}: {_e}")
        except Exception as e:
            print(f"[PRIV] Room scan error: {e}")

    async def on_user_join(self, user, position=None, *args, **kwargs):
        print(f"{user.username} joined the room")
        try:
            # Auto-detect room moderators/designers
            try:
                priv_resp = await self.highrise.get_room_privilege(user.id)
                perms = priv_resp.content
                if perms.moderator or perms.designer:
                    self.room_moderators.add(user.username.lower())
                    # Add to ADMINS list so all existing checks work automatically
                    if user.username not in self.ADMINS:
                        self.ADMINS.append(user.username)
                    print(f"[PRIV] {user.username} detected as room moderator/designer")
                else:
                    self.room_moderators.discard(user.username.lower())
            except Exception as _e:
                print(f"[PRIV] Could not check privilege for {user.username}: {_e}")
            # Visit Tracking
            try:
                room_id = self.room_id
                
                if room_id not in self.user_visits: self.user_visits[room_id] = {}
                if user.username not in self.user_visits[room_id]: self.user_visits[room_id][user.username] = 0
                self.user_visits[room_id][user.username] += 1
                self.settings["user_visits"] = self.user_visits

                # Record first_seen in user_stats if not present
                if user.username not in self.user_stats:
                    self.user_stats[user.username] = {"first_seen": time.strftime("%Y-%m-%d %H:%M:%S")}
                elif "first_seen" not in self.user_stats[user.username]:
                    self.user_stats[user.username]["first_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.settings["user_stats"] = self.user_stats

                self.save_settings()
            except Exception as e:
                print(f"Visit tracking error: {e}")

            # Time Tracking Session Start
            if self.time_tracking:
                self.join_times[user.id] = time.time()

            # Custom Welcome Message Logic
            custom_msg = self.settings.get("custom_welcome_message")
            if custom_msg:
                room_id = self.room_id
                visit_count = self.user_visits.get(room_id, {}).get(user.username, 1)
                message = custom_msg.replace('@username', f"@{user.username}")
                message = message.replace('{username}', user.username)
                message = message.replace('{room}', self.current_room_name)
                message = message.replace('{date}', time.strftime("%Y-%m-%d"))
                message = message.replace('{visits}', str(visit_count))
            elif user.username.lower() in [o.lower() for o in self.OWNERS]:
                import random as _r
                owner_welcomes = [
                    f"<#FF4500>🔥👑 صاحب الروم @{user.username} وصل! <#FFD700>مقزار روم مستنياك يا أسطورة! 🎉",
                    f"<#FFD700>👑 يا هلا يا @{user.username}! <#FF6600>🔥 ملك مقزار روم رجع لبيته! 🎊",
                    f"<#FFFFFF>⭐ @{user.username} جاء! <#C0C0C0>🌟 صاحب الروم نوّر مقزار روم! <#FFD700>👑",
                    f"<#00CED1>❄️ @{user.username} وصل! <#FFD700>👑 <#FF4500>🔥 الأوضة كانت ناقصة صاحبها! 💫",
                    f"<#FF6600>🔥 يا سلام! <#FFFFFF>صاحب الروم @{user.username}! <#FFD700>👑 مقزار روم بيك تكتمل! 🎶",
                ]
                message = _r.choice(owner_welcomes)
            elif user.id == "6805a4cd50458cdb77eedf49":
                import random as _r
                special_meedo = [
                    f"<#FFD700>👑 يا سلام! ميدو الأسطورة وصل لـ مقزار روم! 🎉 أهلاً بالغالي @{user.username}!",
                    f"<#FF4500>🔥 يا هلا يا ميدو! مقزار روم ما كانت ناقصة غير وجودك! 💫 @{user.username}",
                    f"<#00CED1>❄️ الملك وصل! @{user.username} في مقزار روم وكلنا مبسوطين! 🎊",
                    f"<#FFFFFF>⭐ ميدو جاء وكملت السهرة! أهلاً وسهلاً @{user.username} في مكانك الصح! 🎶",
                    f"<#C0C0C0>🌟 يا أهلاً بميدو! @{user.username} زين الحضور في مقزار روم! <#FFD700>👑",
                ]
                message = _r.choice(special_meedo)
            elif user.id == "658f83c9a32f0ae61c072a6f":
                import random as _r
                special_aboreda = [
                    f"<#FFD700>👑 أبو رضا وصل لـ مقزار روم! يا هلا بالأصيل @{user.username}! 🎊",
                    f"<#FF4500>🔥 يا مرحبا يا أبو رضا! @{user.username} مقزار روم بيك أجمل! 💫",
                    f"<#00CED1>❄️ الحضور الكريم وصل! @{user.username} أهلاً بيك في مقزار روم! <#FFD700>👑",
                    f"<#FFFFFF>⭐ أبو رضا في الدار! @{user.username} يسعد سهرتنا وجودك! 🎉",
                    f"<#C0C0C0>🌟 يا هلا يا أبو رضا! @{user.username} مقزار روم ما استوت إلا بيك! <#FF4500>🔥",
                ]
                message = _r.choice(special_aboreda)
            else:
                all_greetings = [
                    # 🔥 ناري
                    f"<#FF4500>🔥 أهلاً وسهلاً @{user.username} في مقزار روم! يلا استمتع معانا 🎶",
                    f"<#FF6600>🔥 @{user.username} نوّر مقزار روم! حللت أهلاً وحللت سهلاً 💫",
                    f"<#FF0000>🔥 يا هلا يا @{user.username}! في مقزار روم النار ما بتهدأش 👑🎉",
                    f"<#FF4500>🎤 يا أهلاً يا @{user.username}! مقزار روم اشتعلت بوجودك 🔥",
                    f"<#FF6600>🎊 @{user.username} وصل! مقزار روم دايماً ناار 🔥🎉",
                    # ❄️ تلجي
                    f"<#00CED1>❄️ يا مرحبا @{user.username}! مقزار روم بتتشرف بيك 💎",
                    f"<#87CEEB>❄️ @{user.username} وصل! مقزار روم بقت أحلى بوجودك 😍",
                    f"<#ADD8E6>❄️ أهلاً @{user.username}! انت في أجمل سهرة في هايرايز - مقزار روم! 🌙",
                    f"<#00CED1>💎 @{user.username} نوّر المكان! مقزار روم في أحسن حالاتها دلوقتي ❄️",
                    f"<#87CEEB>🌊 يا هلا بـ@{user.username}! تفضل استمتع في مقزار روم 💃",
                    # 💛 جولد
                    f"<#FFD700>🏆 @{user.username} وصل! مقزار روم مستنياك من زمان 🎉",
                    f"<#FFC000>👑 حياك الله @{user.username}! مقزار روم ترحب بيك أحسن ترحيب 🌟",
                    f"<#FFD700>💛 @{user.username} جاء! مقزار روم معاك هتبقى أحلى 🎶",
                    f"<#FFC000>🌙 @{user.username} نوّر! مقزار روم دايما بخير وانت بين ضهرانينا 🎊",
                    f"<#FFD700>🥳 يا مرحبا @{user.username}! الله يعطيك العافية وانت تزيّن مقزار روم 👑",
                    # ⭐ أبيض
                    f"<#FFFFFF>⭐ @{user.username} دخل! مقزار روم مش هتتكمل إلا بيك 🎶",
                    f"<#FFFFFF>✨ حياك الله @{user.username}! مقزار روم تشرفت بحضورك 🌙",
                    f"<#FFFFFF>🌟 يا هلا والله @{user.username}! ورّيت علينا في مقزار روم 🎉",
                    f"<#FFFFFF>💫 أهلاً @{user.username}! مقزار روم ملكها الأصيل رجع 🎉",
                    f"<#FFFFFF>🎵 @{user.username} حضر! مقزار روم كملت بوجودك يا غالي 💖",
                    # 🌫️ فضي
                    f"<#C0C0C0>🌟 حياك ربي @{user.username}! مقزار روم ما كانت ناقصة غيرك 👑",
                    f"<#C0C0C0>✨ يا مرحبا @{user.username}! تفضل وانبسط في مقزار روم 💃",
                    f"<#C0C0C0>🎶 @{user.username} نوّر! مقزار روم مع كل ضيف تصير أجمل 🌸",
                    f"<#C0C0C0>💎 حياك الله @{user.username}! ربع مقزار روم كلهم سعادتهم بيك 🎊",
                    f"<#C0C0C0>🌙 يا أهلاً بـ@{user.username}! استمتع في مقزار روم اللي ما في زيها 🌟",
                ]
                message = random.choice(all_greetings)
            
            await self.highrise.chat(message)

            # Auto Heart Reaction
            try:
                await self.highrise.react("heart", user.id)
            except Exception as e:
                print(f"Error sending heart reaction to {user.username}: {e}")
            
            # VIP Join Message
            is_vip_msg_sent = False
            if self.vip_message_enabled and user.username in self.VIPS:
                await self.highrise.chat(f"👑 Welcome VIP @{user.username}! Everyone please welcome our VIP! 👑")
                is_vip_msg_sent = True
            
            # Owner/Admin Join Message (Only if VIP message not already sent, to avoid spam)
            if not is_vip_msg_sent:
                if self.owner_message_enabled and user.username in self.OWNERS:
                    await self.highrise.chat(f"👤 OWNER @{user.username} has entered the room! Welcome! 👑")
                elif self.admin_message_enabled and user.username in self.ADMINS and user.username not in self.OWNERS:
                    await self.highrise.chat(f"🛡️ ADMIN @{user.username} has entered the room! 🛡️")
            
            # Track Initial Position
            if isinstance(position, Position):
                self.user_positions[user.id] = position

            # Streak Logic (silent - no chat messages)
            try:
                username = user.username
                today_str = datetime.now().strftime("%Y-%m-%d")
                if username not in self.user_streaks:
                    self.user_streaks[username] = {"streak": 1, "last_seen": today_str}
                else:
                    data = self.user_streaks[username]
                    last_seen = data.get("last_seen")
                    if last_seen != today_str:
                        last_date = datetime.strptime(last_seen, "%Y-%m-%d")
                        current_date = datetime.strptime(today_str, "%Y-%m-%d")
                        delta = (current_date - last_date).days
                        if delta == 1:
                            data["streak"] += 1
                            data["last_seen"] = today_str
                        else:
                            data["streak"] = 1
                            data["last_seen"] = today_str
                self.settings["user_streaks"] = self.user_streaks
                self.save_settings()
            except Exception as e:
                print(f"Streak error: {e}")

            
        except Exception as e:
            print(f"Error in on_user_join: {e}")

    async def on_user_move(self, user, pos):
        """Track movement and handle Flash Teleport."""
        # Update position tracking
        old_pos = self.user_positions.get(user.id)
        self.user_positions[user.id] = pos
        
        # Flash Teleport Logic
        if user.username in self.flash_users and old_pos:
            if isinstance(pos, Position) and isinstance(old_pos, Position):
                # Calculate distance
                dx = pos.x - old_pos.x
                dy = pos.y - old_pos.y
                dz = pos.z - old_pos.z
                dist = (dx*dx + dy*dy + dz*dz)**0.5
                
                if dist > 4:
                    # Teleport instantly
                    try:
                        await self.highrise.teleport(user.id, pos)
                    except Exception as e:
                        print(f"Flash tele error: {e}")

        # Freeze Logic
        if user.id in self.frozen_users:
            frozen_pos = self.frozen_users[user.id]
            if abs(pos.x - frozen_pos.x) > 0.1 or abs(pos.y - frozen_pos.y) > 0.1 or abs(pos.z - frozen_pos.z) > 0.1:
                await self.highrise.teleport(user.id, frozen_pos)

    async def on_chat(self, user, message):
        try:
            trans = self.translations.get(self.language, self.translations["english"])
            print(f"{user.username} said: {message}")
            
            # Chat Tracking Log
            if self.chat_tracking:
                try:
                    with open("chat_logs.txt", "a", encoding="utf-8") as f:
                        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                        f.write(f"[{timestamp}] {user.username}: {message}\n")
                except Exception as e:
                    print(f"Logging error: {e}")

            # Track Chat Stats
            self.chat_stats[user.username] = self.chat_stats.get(user.username, 0) + 1
            self.settings["chat_stats"] = self.chat_stats
            
            # Track Command Usage
            if message.startswith("!"):
                self.command_usage[user.username] = self.command_usage.get(user.username, 0) + 1
                self.settings["command_usage"] = self.command_usage
            
            # BLOCKED USER CHECK - COMMAND LOCK
            # Block ANY message starting with "!" (commands) or if they are trying to use the bot
            # We also block them from normal chatting if you want "all access locked", but usually bots only control bot usage.
            # The user said "not use public command and owner and moderator owner command all acess locked"
            # This implies they shouldn't trigger ANYTHING.
            if user.username in self.blocked_users:
                 if message.startswith("!"):
                      return

            msg_lower = message.strip().lower()

            # --- BOT MANAGEMENT (OWNER ONLY) ---
            if msg_lower.startswith("!botadd"):
                if any(o.lower() == user.username.lower() for o in self.OWNERS):
                     try:
                         parts = message.split()
                         if len(parts) < 3:
                             await self.highrise.chat("💡 Usage: !botadd [token] [room_id/link]")
                             return
                         token = parts[1].strip()
                         room_arg = parts[2].strip()
                         
                         rid = room_arg
                         if "highrise.game/room/" in room_arg:
                            rid = room_arg.split("highrise.game/room/")[-1].split("?")[0].split("&")[0]
                         elif "high.rs/room?id=" in room_arg:
                            rid = room_arg.split("high.rs/room?id=")[-1].split("&")[0]

                         bot_name = f"Bot_{int(time.time() % 10000)}"
                         bot_data = {"id": str(int(time.time() * 1000)), "name": bot_name, "room_id": rid, "token": token, "status": "running"}
                         
                         bots = []
                         if os.path.exists("bots_config.json"):
                             with open("bots_config.json", "r") as f: bots = json.load(f)
                         bots.append(bot_data)
                         with open("bots_config.json", "w") as f: json.dump(bots, f, indent=4)
                         
                         await self.highrise.chat(f"🚀 Bot '{bot_name}' PERMANENTLY ADDED to config! It will join Room {rid} shortly. ✨")
                     except Exception as e:
                         await self.highrise.chat(f"❌ Error: {e}")
                else: await self.highrise.chat("❌ Owner only!")
                return


            elif msg_lower == "!sub":
                return

                
            # Periodic Save (Every 10 messages from this user to optimize)
            if self.chat_stats[user.username] % 10 == 0:
                self.save_settings()

            # Command Lock System - Must !sub first
            msg_lower = message.strip().lower()
            is_start_cmd = message.startswith("!")
            # Check if user is typing an emote name (handles multi-word names)
            is_digit = msg_lower.isdigit()
            is_emote_name = False
            
            # Use the same logic as the trigger system to check if it's an emote
            temp_clean = msg_lower[1:] if msg_lower.startswith("!") else msg_lower
            # Check for target at end
            if " " in temp_clean:
                parts = temp_clean.split()
                if parts[-1].startswith("@") or parts[-1] == "all":
                    temp_clean = " ".join(parts[:-1])
            
            if temp_clean in self.emotes or temp_clean in self.friendly_emotes or msg_lower == "emotelist":
                is_emote_name = True
            
            is_any_cmd = is_start_cmd or is_digit or is_emote_name or msg_lower in ["stop", "stop emote"]
            bypass_cmds = ["!menu", "!setting", "!help", "!ping", "emotelist", "!stop", "stop", "stop emote", "!stop emote", "!flash", "!mytickets", "!تذاكري", "!تذكرتي", "!رصيدي", "!radio", "!راديو", "!مكانك", "مكانك", "!موقفك", "موقفك", "!p ", "!q", "!queue", "!s", "كف", "!list", "!yes", "!no", "!اه", "!نعم", "!ايوه", "!لا", "!la", "!الغاء", "!إلغاء", "!mex", "!جاسوس", "!spy", "!كشف", "!reveal", "!صراحة", "!صراحه", "!truth", "!جرأة", "!جراءه", "!جراه", "!dare", "!سرعة", "!سرعه", "!speed", "!سؤال", "!سوال", "!quiz", "!games", "!ألعاب", "!العاب"]
            if msg_lower.startswith("h ") and (user.username in self.OWNERS or user.username in self.admins):
                bypass_cmds.append("h ")
            

            # Check if user is subscribed (handle both string and dict formats)
            # Check if user is subscribed (handle both string and dict formats)
            is_subbed = False
            for s in self.subscribers:
                if isinstance(s, dict):
                    if s.get("id") == user.id or s.get("username", "").lower() == user.username.lower():
                        is_subbed = True
                        break
                elif isinstance(s, str) and s.lower() == user.username.lower():
                    is_subbed = True
                    break

            # Check VIP status (including expiration)
            is_vip_active = False
            if isinstance(self.VIPS, dict):
                if user.username in self.VIPS:
                    exp = self.VIPS[user.username]
                    if exp == "permanent" or (isinstance(exp, (int, float)) and exp > time.time()):
                        is_vip_active = True
            elif isinstance(self.VIPS, list):
                if user.username in self.VIPS:
                    is_vip_active = True

            _all_privileged = [a.lower() for a in self.ADMINS + self.OWNERS + self.hardcoded_owners] + list(self.room_moderators)
            if is_any_cmd and user.username.lower() not in _all_privileged:
                # Emotes and numbers are free for everyone
                if is_emote_name or is_digit:
                    pass
                else:
                    is_bypass = False
                    for cmd in bypass_cmds:
                        if msg_lower.startswith(cmd):
                            is_bypass = True
                            break
                    if not is_bypass:
                        await self.highrise.chat(f"❌ <#FF4500>@{user.username} هذا الأمر للمشرفين فقط! 🔒")
                        return

            # --- STOP COMMAND ---
            if msg_lower in ["stop", "stop emote", "!stop", "!stop emote", "0"]:
                if user.id in self.looping_users or self.bot_id in self.looping_users:
                    self.looping_users.pop(user.id, None)
                    # Also stop bot loop if caller is the one who likely started it or is admin
                    if user.username in self.ADMINS or user.id in self.looping_users:
                         self.looping_users.pop(self.bot_id, None)
                    await self.highrise.chat(f"<#FF0000>🛑 وقفت الرقصة!")
                else:
                    await self.highrise.chat("⚠️ مش في رقصة حالياً.")
                return

            # Intercept yes/no (with or without !) for pending playlist BEFORE emote system
            if user.username in self.pending_playlist:
                _yes_words = ("yes", "y", "!yes", "!اه", "!نعم", "!ايوه", "اه", "نعم", "ايوه", "ok", "اوك")
                _no_words = ("no", "n", "!no", "!لا", "!la", "!الغاء", "!إلغاء", "لا", "la", "cancel")
                if msg_lower in _yes_words:
                    pending = self.pending_playlist.get(user.username)
                    if pending:
                        genre = pending["genre"]
                        songs = pending["songs"]
                        del self.pending_playlist[user.username]
                        genre_name = radio_playlists.genre_display_name(genre)
                        await self.highrise.chat(f"<#00CED1>🎵 بيضيف {min(len(songs),20)} أغنية من {genre_name}...")
                        added = 0
                        for s in songs[:20]:
                            try:
                                async with aiohttp.ClientSession() as session:
                                    async with session.post(
                                        f"{self.RADIO_API_URL}/api/radio/add",
                                        json={"title": s["title"], "url": s["url"], "uploader": s.get("uploader",""), "duration": s.get("duration",0), "requestedBy": f"playlist:{genre}"},
                                        timeout=aiohttp.ClientTimeout(total=5)
                                    ) as resp:
                                        if resp.status == 200:
                                            added += 1
                            except Exception:
                                pass
                        await self.highrise.chat(f"<#00FF00>✅ اتضافوا {added} أغنية من {genre_name} للقائمة! 🎶")
                    return
                elif msg_lower in _no_words:
                    pending = self.pending_playlist.pop(user.username, None)
                    if pending:
                        genre_name = radio_playlists.genre_display_name(pending["genre"])
                        await self.highrise.chat(f"<#C0C0C0>❌ تم إلغاء تشغيل {genre_name}")
                    return

            # Unified Emote & Targeting System (Improved for multi-word emotes)
            parts = message.split()
            if parts:
                msg_lower = message.strip().lower()
                clean_msg = msg_lower[1:] if msg_lower.startswith("!") else msg_lower
                
                emote_id = None
                friendly_name = None
                target_id = user.id
                target_name = user.username
                is_targeted = False
                is_all = False

                # 1. Resolve Target and Emote Name
                if len(parts) >= 2:
                    potential_target = parts[-1].lower()
                    if potential_target.startswith("@"):
                        req_target = potential_target[1:]
                        room_users = (await self.highrise.get_room_users()).content
                        for u, _ in room_users:
                            if u.username.lower() == req_target:
                                target_id = u.id
                                target_name = u.username
                                is_targeted = True
                                # Exclude target from emote name search
                                clean_msg = " ".join(parts[:-1]).lower()
                                if clean_msg.startswith("!"): clean_msg = clean_msg[1:]
                                break
                    elif potential_target == "all" and user.username in self.ADMINS:
                        is_all = True
                        is_targeted = True
                        clean_msg = " ".join(parts[:-1]).lower()
                        if clean_msg.startswith("!"): clean_msg = clean_msg[1:]

                # 2. Resolve Emote ID
                if clean_msg.isdigit():
                    try:
                        idx_num = int(clean_msg) - 1
                        if 0 <= idx_num < len(self.emote_list):
                            friendly_name = self.emote_list[idx_num]
                            emote_id = self.emotes.get(friendly_name)
                    except: pass
                elif clean_msg in self.emotes:
                    friendly_name = clean_msg
                    emote_id = self.emotes.get(clean_msg)
                elif clean_msg in self.friendly_emotes:
                    friendly_name = clean_msg
                    emote_id = self.friendly_emotes.get(clean_msg)
                    # Try to find the original name from self.emotes for better display
                    for k, v in self.emotes.items():
                        if v == emote_id and not k.isdigit():
                            friendly_name = k
                            break
                elif clean_msg.startswith("emote "):
                    req = clean_msg[6:].strip()
                    if req in self.emotes:
                        friendly_name = req
                        emote_id = self.emotes.get(req)
                    elif req in self.friendly_emotes:
                        friendly_name = req
                        emote_id = self.friendly_emotes.get(req)

                if emote_id:
                    # 3. Execute
                    try:
                        idx = self.emote_list.index(friendly_name) + 1 if friendly_name in self.emote_list else "?"
                        if is_all:
                            room_users = (await self.highrise.get_room_users()).content
                            for u, _ in room_users:
                                self.looping_users[u.id] = emote_id
                                try:
                                    await self.highrise.send_emote(emote_id, u.id)
                                except: pass
                            await self.highrise.chat(f"🌈 <#FF00FF>Global Loop: <#FFFFFF>#{idx} <#00FFFF>{friendly_name.upper()} for Everyone! 💫")
                        elif is_targeted:
                            await self.highrise.chat(f"✨ <#FFFF00>@{user.username} <#00FF00>triggered <#FFFFFF>#{idx} <#FF69B4>{friendly_name.upper()} <#00FFFF>on @{target_name}! 🔄")
                            await self.highrise.send_emote(emote_id, target_id)
                            self.looping_users[target_id] = emote_id
                        else:
                            await self.highrise.chat(f"✨ <#FFFF00>@{user.username} <#00FF00>started <#FFFFFF>#{idx} <#FF69B4>{friendly_name.upper()}! 🔄 (Looping)")
                            await self.highrise.send_emote(emote_id, target_id)
                            self.looping_users[target_id] = emote_id
                        
                        return # Stop processing
                    except Exception as e:
                        print(f"Emote Error: {e}")

            msg_lower = message.strip().lower()
            
            if msg_lower.startswith("!autotip"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    if len(parts) == 1:
                         await self.highrise.chat("💡 Usage: !autotip [on/off/status/time/amount]")
                         return
                    
                    subcmd = parts[1].lower()
                    if subcmd == "on":
                        self.auto_tip = True
                        self.settings["auto_tip"] = True
                        self.save_settings()
                        interval = getattr(self, "autotip_interval", 600)
                        amount = getattr(self, "autotip_amount", 1)
                        await self.highrise.chat(f"✅ <#00FF00>Automatic Tipping System is now ON! (Every {interval}s, {amount}G)")
                    elif subcmd == "off":
                        self.auto_tip = False
                        self.settings["auto_tip"] = False
                        self.save_settings()
                        await self.highrise.chat("❌ <#FF0000>Automatic Tipping System is now OFF!")
                    elif subcmd == "status":
                        status = "ON ✅" if getattr(self, "auto_tip", False) else "OFF ❌"
                        interval = getattr(self, "autotip_interval", 600)
                        amount = getattr(self, "autotip_amount", 1)
                        await self.highrise.chat(f"📊 <#00FFFF>Auto-Tip Status:\nStatus: {status}\nInterval: {interval}s\nAmount: {amount} Gold")
                    elif subcmd == "time":
                        if len(parts) > 2:
                            try:
                                interval = int(parts[2])
                                if interval < 30:
                                    await self.highrise.chat("⚠️ Minimum interval is 30 seconds.")
                                    return
                                self.autotip_interval = interval
                                self.settings["autotip_interval"] = interval
                                self.save_settings()
                                await self.highrise.chat(f"🕒 <#00FF00>Auto-Tip interval set to {interval} seconds!")
                            except:
                                await self.highrise.chat("❌ Usage: !autotip time [seconds]")
                        else:
                            await self.highrise.chat("❌ Usage: !autotip time [seconds]")
                    elif subcmd == "amount":
                        if len(parts) > 2:
                            try:
                                amount = int(parts[2])
                                tip_ids = ["1", "5", "10", "50", "100", "500", "1000"]
                                if str(amount) not in tip_ids:
                                    await self.highrise.chat(f"❌ Invalid amount! Choose from: {', '.join(tip_ids)}")
                                    return
                                self.autotip_amount = amount
                                self.settings["autotip_amount"] = amount
                                self.save_settings()
                                await self.highrise.chat(f"💰 <#00FF00>Auto-Tip amount set to {amount} Gold!")
                            except:
                                await self.highrise.chat("❌ Usage: !autotip amount [gold]")
                        else:
                            await self.highrise.chat("❌ Usage: !autotip amount [gold]")
                    else:
                        await self.highrise.chat("💡 Usage: !autotip [on/off/status/time/amount]")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return

            if msg_lower.startswith("!punch"):
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.chat("💡 Usage: !punch @username")
                    return
                
                target_name = parts[1].replace("@", "").lower()

                # If user tries to punch themselves
                if target_name == user.username.lower():
                    await self.highrise.chat(f"🤔 @{user.username} punches themselves... Are you okay?")
                    try:
                        await self.highrise.send_emote("emoji-punch", user.id)
                    except: pass
                    return

                room_users = (await self.highrise.get_room_users()).content
                target_id = None
                target_display_name = None
                for r_user, _ in room_users:
                    if r_user.username.lower() == target_name:
                        target_id = r_user.id
                        target_display_name = r_user.username
                        break
                
                if not target_id:
                    await self.highrise.chat(f"❌ User @{parts[1].replace('@', '')} not found in the room.")
                    return
                
                # Check if punching the bot
                if target_id == getattr(self, "bot_id", None):
                    await self.highrise.chat(f"🛡️ *Matrix dodges*! You can't punch me, @{user.username}!")
                    try:
                         # Send a random emote for dodging to bot if possible, otherwise just chat response
                         pass
                    except: pass
                    return

                initiator_emote = "emoji-punch"
                target_emote = "emote-deathdrop"
                
                punch_messages = [
                    f"👊 @{user.username} lands a solid punch on @{target_display_name}!",
                    f"🥊 @{user.username} knocks out @{target_display_name} with a left hook!",
                    f"💥 BAM! @{user.username} just punched @{target_display_name} into next week!",
                    f"💫 @{target_display_name} didn't even see @{user.username}'s punch coming!"
                ]
                punch_message = random.choice(punch_messages)
                
                try:
                    await self.highrise.send_emote(initiator_emote, user.id)
                    await self.highrise.send_emote(target_emote, target_id)
                except Exception:
                    pass
                
                await self.highrise.chat(punch_message)
                return

            if msg_lower.startswith("كف "):
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.chat("💡 Usage: كف @username")
                    return

                target_name = parts[1].replace("@", "").lower()

                if target_name == user.username.lower():
                    await self.highrise.chat(f"🤨 @{user.username} حاول يكف نفسه... مش هينفع!")
                    return

                room_users = (await self.highrise.get_room_users()).content
                target_id = None
                target_display_name = None
                target_pos = None
                for r_user, r_pos in room_users:
                    if r_user.username.lower() == target_name:
                        target_id = r_user.id
                        target_display_name = r_user.username
                        target_pos = r_pos
                        break

                if not target_id:
                    await self.highrise.chat(f"❌ مش لاقي @{parts[1].replace('@','')} في الروم.")
                    return

                if target_id == getattr(self, "bot_id", None):
                    await self.highrise.chat(f"🛡️ ما تكفنيش يا @{user.username}! أنا بوت مش خايف 😎")
                    return

                # Freeze the target in their current position
                if hasattr(target_pos, 'x'):
                    self.frozen_users[target_id] = target_pos

                kef_messages = [
                    f"✏️ @{user.username} كف @{target_display_name} بالقلم! 🤕",
                    f"✏️ @{target_display_name} اتكف بالقلم من @{user.username}! 😵",
                    f"💥 @{user.username} كفّ @{target_display_name} كف نظيف بالقلم! ✏️",
                    f"🖊️ @{user.username} ضرب @{target_display_name} بالقلم وفضّل مكانه! 😳",
                ]
                kef_msg = random.choice(kef_messages)

                try:
                    await self.highrise.send_emote("dance-voguehands", user.id)
                    await self.highrise.send_emote("emote-deathdrop", target_id)
                except Exception:
                    pass

                await self.highrise.chat(kef_msg)

                # Unfreeze after 8 seconds
                async def unfreeze_later(uid):
                    await asyncio.sleep(8)
                    if uid in self.frozen_users:
                        del self.frozen_users[uid]

                asyncio.create_task(unfreeze_later(target_id))
                return

            if msg_lower == "كف":
                try:
                    await self.highrise.send_emote("dance-voguehands", user.id)
                except Exception:
                    pass
                return

            # ============================================================
            # ============== 🎮 GAMES SECTION 🎮 =========================
            # ============================================================
            # Lazy-init game state
            if not hasattr(self, "_games_init"):
                self._games_init = True
                self.spy_game = None          # {"word": str, "spy": username, "players": [usernames], "host": username}
                self.speed_game = None        # {"word": str, "host": username, "started_at": float}
                self.quiz_game = None         # {"question": str, "answer": str, "host": username, "started_at": float}
                self.SPY_WORDS = [
                    "مدرسة", "مستشفى", "مطار", "شاطئ", "ملعب كرة", "سينما", "مطعم",
                    "حديقة حيوان", "بنك", "فندق", "صيدلية", "مكتبة", "محطة قطار",
                    "سوبر ماركت", "صالون حلاقة", "نادي رياضي", "كافيه", "متحف",
                    "محطة بنزين", "مصنع", "مزرعة", "كنيسة", "جامع", "قصر", "كهف",
                    "غابة", "صحراء", "جبل", "نهر", "بحر"
                ]
                self.TRUTH_QS = [
                    "إيه أكتر حاجة بتخاف منها؟",
                    "مين أول حد عجبك في الروم دي؟",
                    "إيه أكبر سر بتخفيه عن أهلك؟",
                    "لو لازم تسيب الهايرايز يوم، هتعمل إيه؟",
                    "أكتر موقف محرج حصلك إيه؟",
                    "بتحب مين هنا أكتر واحد؟",
                    "إيه آخر كدبة قلتها؟",
                    "لو معاك مليون جنيه هتعمل بيهم إيه؟",
                    "إيه أغبى حاجة عملتها في حياتك؟",
                    "مين الشخص اللي مش طايقه في الروم؟ (من غير ما تقول اسم 😅)",
                    "بتحلم بإيه دايماً؟",
                    "آخر مرة عيطت فيها كانت إمتى وليه؟"
                ]
                self.DARE_QS = [
                    "ابعت رسالة حب لأي حد في الروم 💌",
                    "اعمل رقصة dance-tiktok2 قدام الكل 💃",
                    "غير اسمك لمدة دقيقة لـ 'أنا أحبك يا بوت' 🤖",
                    "اكتب 5 إيموجي قلب متتالية ❤️❤️❤️❤️❤️",
                    "قول شعر غزل لأي حد في الروم 📝",
                    "اعمل emote-pose7 وفضل واقف دقيقة 🧍",
                    "اكتب أغنية وانت بتغنيها في الشات 🎤",
                    "قول 'أنا أجمل واحد/واحدة في الروم' 3 مرات 😎",
                    "اعمل dance-blackpink قدام الكل 🕺",
                    "هس مين الجاسوس اللي في الروم 🕵️",
                    "اعمل صوت حيوان في الشات 🐱",
                    "قل 3 صفات عن نفسك أمام الجميع ✨"
                ]
                self.QUIZ_BANK = [
                    ("ما هي عاصمة مصر؟", "القاهرة"),
                    ("كم عدد قارات العالم؟", "7"),
                    ("ما هو أكبر محيط في العالم؟", "الهادي"),
                    ("ما هو أطول نهر في العالم؟", "النيل"),
                    ("كم عدد ألوان قوس قزح؟", "7"),
                    ("ما هو الكوكب الأحمر؟", "المريخ"),
                    ("من اخترع المصباح الكهربائي؟", "اديسون"),
                    ("ما هي عاصمة فرنسا؟", "باريس"),
                    ("كم عدد أيام السنة الكبيسة؟", "366"),
                    ("ما هو أصغر كوكب في المجموعة الشمسية؟", "عطارد"),
                    ("ما هي أكبر قارة في العالم؟", "آسيا"),
                    ("كم عدد لاعبي كرة القدم في الفريق؟", "11"),
                    ("ما اسم البحر بين مصر والسعودية؟", "الاحمر"),
                    ("في أي عام بدأت الحرب العالمية الثانية؟", "1939"),
                    ("ما هو معدن الذهب رمزه الكيميائي؟", "au")
                ]
                self.SPEED_WORDS = [
                    "نجم", "قمر", "شمس", "بحر", "كتاب", "قلم", "وردة", "عصفور",
                    "سحابة", "مطر", "ثلج", "نار", "ضوء", "طريق", "جسر", "حقل",
                    "غيمة", "نسمة", "حلم", "أمل", "فرحة", "سلام"
                ]

            # ----- !games / !ألعاب : Show all games help -----
            if msg_lower in ("!games", "!ألعاب", "!العاب"):
                games_help = (
                    "🎮 <#FF69B4>قائمة الألعاب 🎮\n"
                    "🕵️ !جاسوس → لعبة الجاسوس (محتاج 3+ لاعبين)\n"
                    "🔓 !كشف → كشف الجاسوس وانهاء اللعبة\n"
                    "💬 !صراحة → سؤال صراحة عشوائي\n"
                    "🔥 !جرأة → تحدي جرأة عشوائي\n"
                    "⚡ !سرعة → أول واحد يكتب الكلمة يكسب\n"
                    "❓ !سؤال → سؤال معلومات عامة"
                )
                await self.highrise.chat(games_help)
                return

            # ----- !جاسوس / !spy : Spy game -----
            if msg_lower in ("!جاسوس", "!spy"):
                if self.spy_game is not None:
                    await self.highrise.chat(f"⚠️ في لعبة جاسوس شغالة فعلاً! اكتب !كشف عشان تخلصها.")
                    return
                room_users = (await self.highrise.get_room_users()).content
                players = [r_user for r_user, _ in room_users if r_user.id != getattr(self, "bot_id", None)]
                if len(players) < 3:
                    await self.highrise.chat("❌ محتاج 3 لاعبين على الأقل عشان تلعب الجاسوس!")
                    return
                word = random.choice(self.SPY_WORDS)
                spy = random.choice(players)
                self.spy_game = {
                    "word": word,
                    "spy": spy.username,
                    "players": [p.username for p in players],
                    "host": user.username
                }
                for p in players:
                    try:
                        if p.username == spy.username:
                            await self.highrise.send_whisper(p.id, "🕵️ <#FF0000>أنت الجاسوس! حاول تخمن الكلمة من اللي اللاعبين بيقولوه ✨")
                        else:
                            await self.highrise.send_whisper(p.id, f"🤫 <#00FF00>الكلمة السرية: <#FFFF00>{word}\n💡 لا تقولها صريحة! وصفها ولاقي الجاسوس!")
                    except Exception:
                        pass
                await self.highrise.chat(f"🎮 <#FF69B4>بدأت لعبة الجاسوس! 🕵️\n👥 {len(players)} لاعبين | كل واحد شوف الهمس\n📢 وصفوا الكلمة من غير ما تقولوها صريحة\n🔚 اكتب !كشف لكشف الجاسوس")
                return

            if msg_lower in ("!كشف", "!reveal"):
                if not self.spy_game:
                    await self.highrise.chat("⚠️ مفيش لعبة جاسوس شغالة دلوقتي.")
                    return
                if user.username not in (self.spy_game["host"], *self.OWNERS, *self.ADMINS):
                    await self.highrise.chat(f"❌ بس صاحب اللعبة @{self.spy_game['host']} يقدر يكشف!")
                    return
                spy = self.spy_game["spy"]
                word = self.spy_game["word"]
                self.spy_game = None
                await self.highrise.chat(f"🕵️ <#FF0000>الجاسوس كان: @{spy}\n🔑 الكلمة كانت: <#FFFF00>{word}")
                return

            # ----- !صراحة / !truth -----
            if msg_lower in ("!صراحة", "!صراحه", "!truth"):
                q = random.choice(self.TRUTH_QS)
                await self.highrise.chat(f"🎯 <#00BFFF>صراحة لـ @{user.username}:\n💬 {q}")
                return

            # ----- !جرأة / !dare -----
            if msg_lower in ("!جرأة", "!جراءه", "!جراه", "!dare"):
                d = random.choice(self.DARE_QS)
                await self.highrise.chat(f"🔥 <#FF4500>تحدي جرأة لـ @{user.username}:\n⚡ {d}")
                return

            # ----- !سرعة / !speed : Speed challenge -----
            if msg_lower in ("!سرعة", "!سرعه", "!speed"):
                if self.speed_game is not None:
                    await self.highrise.chat("⚠️ في تحدي سرعة شغال! خلصوه الأول.")
                    return
                word = random.choice(self.SPEED_WORDS)
                self.speed_game = {"word": word, "host": user.username, "started_at": time.time()}
                await self.highrise.chat(f"⚡ <#FFD700>تحدي السرعة! 🏃\n📝 أول واحد يكتب الكلمة دي يكسب:\n👉 <#FF1493>{word}")
                async def speed_timeout():
                    await asyncio.sleep(30)
                    if self.speed_game and self.speed_game.get("word") == word:
                        self.speed_game = None
                        await self.highrise.chat(f"⏰ خلص الوقت! محدش كتب '{word}' 😢")
                asyncio.create_task(speed_timeout())
                return

            # Speed game answer check
            if self.speed_game and msg_lower.strip() == self.speed_game["word"].lower():
                winner = user.username
                word = self.speed_game["word"]
                self.speed_game = None
                await self.highrise.chat(f"🏆 <#00FF00>@{winner} كسب تحدي السرعة! ⚡\n✅ كلمة '{word}' صح!")
                try:
                    await self.highrise.send_emote("emote-celebrate", user.id)
                except Exception:
                    pass
                return

            # ----- !سؤال / !quiz : Q&A Trivia -----
            if msg_lower in ("!سؤال", "!سوال", "!quiz"):
                if self.quiz_game is not None:
                    await self.highrise.chat("⚠️ في سؤال شغال! جاوبوا الأول.")
                    return
                q, a = random.choice(self.QUIZ_BANK)
                self.quiz_game = {"question": q, "answer": a.lower(), "host": user.username, "started_at": time.time()}
                await self.highrise.chat(f"❓ <#9370DB>سؤال:\n📚 {q}\n💡 أول إجابة صح تكسب! (عندك 45 ثانية)")
                async def quiz_timeout():
                    await asyncio.sleep(45)
                    if self.quiz_game and self.quiz_game.get("answer") == a.lower():
                        self.quiz_game = None
                        await self.highrise.chat(f"⏰ خلص الوقت! الإجابة الصحيحة: <#00FF00>{a}")
                asyncio.create_task(quiz_timeout())
                return

            # Quiz answer check
            if self.quiz_game:
                guess = msg_lower.strip().replace("ال", "").strip()
                correct = self.quiz_game["answer"].replace("ال", "").strip()
                if guess == correct or msg_lower.strip() == self.quiz_game["answer"]:
                    winner = user.username
                    ans = self.quiz_game["answer"]
                    self.quiz_game = None
                    await self.highrise.chat(f"🎉 <#00FF00>@{winner} جاوب صح! ✅\n📖 الإجابة: {ans}")
                    try:
                        await self.highrise.send_emote("emote-celebrate", user.id)
                    except Exception:
                        pass
                    return
            # ============================================================
            # ============= END OF GAMES SECTION =========================
            # ============================================================

            if msg_lower.startswith("!transport"):
                if user.username in self.OWNERS:
                    parts = message.split()
                    if len(parts) < 3:
                        await self.highrise.chat("Usage: !transport @username [room] OR !transport all [room]")
                        return
                    
                    target_input = parts[1]
                    room_input = parts[2]
                    
                    # Clean Room ID
                    new_room_id = room_input
                    if "highrise.game/room/" in room_input:
                        new_room_id = room_input.split("highrise.game/room/")[-1].split("?")[0].split("&")[0]
                    elif "high.rs/room?id=" in room_input:
                        new_room_id = room_input.split("high.rs/room?id=")[-1].split("&")[0]

                    # 1. Transport ALL
                    if target_input.lower() == "all":
                        await self.highrise.chat(f"🚀 Transporting EVERYONE to {new_room_id}... 🌌")
                        try:
                            room_users = (await self.highrise.get_room_users()).content
                            for r_user, _ in room_users:
                                if r_user.id == self.bot_id: continue
                                try:
                                    # Try to force move the user
                                    try:
                                        await self.highrise.move_user_to_room(r_user.id, new_room_id)
                                    except:
                                        # Fallback to whisper link
                                        await self.highrise.send_whisper(r_user.id, f"🚀 Portal Opened! Tap: https://highrise.game/room/{new_room_id}")
                                    await asyncio.sleep(0.5)
                                except: pass
                        except Exception as e:
                            print(f"Transport all error: {e}")

                    # 2. Transport Single User
                    else:
                        target_name = target_input.replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        target_id = None
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name.lower():
                                target_id = r_user.id
                                break
                        
                        if target_id:
                            await self.highrise.chat(f"🚀 Transporting @{target_name} to {new_room_id}... 🌌")
                            try:
                                # Try to force move
                                await self.highrise.move_user_to_room(target_id, new_room_id)
                            except Exception as e:
                                # Fallback to whisper
                                try:
                                    await self.highrise.send_whisper(target_id, f"🚀 Portal Opened! Tap: https://highrise.game/room/{new_room_id}")
                                except: pass
                        else:
                            await self.highrise.chat(f"❌ User @{target_name} not found.")
                else:
                    await self.highrise.chat("❌ Owner only!")
                return

            # Loop Message System Commands
            if msg_lower.startswith("!loop"):
                if user.username in self.ADMINS:
                    parts = message.split(" ", 2)
                    if len(parts) == 1:
                        # Show usage
                        usage = (
                            "<#00FFFF>🔁 LOOP MESSAGE SYSTEM 🔁\n"
                            "<#FFFF00>Usage:\n"
                            "<#FFFFFF>!loop (number) (message) - Set a loop message\n"
                            "<#FFFFFF>!loop (number) - Clear permanent loop message\n"
                            "<#FFFFFF>!msgtime (number) (seconds) - Set time for slot\n"
                            "<#888888>Example: !loop 1 hello everyone\n"
                            "<#888888>Example: !msgtime 1 30\n"
                            "<#888888>Example: !loop 1 (to clear slot 1)\n\n"
                            "<#00FF00>📋 Current Loop Messages:\n"
                        )
                        if self.loop_messages:
                            for slot, msg in sorted(self.loop_messages.items()):
                                interval = self.loop_intervals.get(slot, self.loop_cooldown)
                                usage += f"<#FFFF00>Slot {slot}: <#FFFFFF>{msg} <#888888>({interval}s)\n"
                        else:
                            usage += "<#888888>No loop messages set.\n"
                        
                        usage += f"\n<#00FFFF>Status: <#{'00FF00' if self.loop_running else 'FF0000'}>{('RUNNING ✅' if self.loop_running else 'PAUSED ❌')}\n"
                        usage += f"<#00FFFF>Default Cooldown: <#FFFF00>{self.loop_cooldown}s"
                        await self.safe_chat(usage)
                        return
                    
                    try:
                        slot_num = parts[1]
                        
                        # Validate slot number
                        if not slot_num.isdigit():
                            await self.highrise.chat("❌ Slot number must be a number (1, 2, 3, etc.)")
                            return
                        
                        if len(parts) == 2:
                            # Clear the loop message
                            if slot_num in self.loop_messages:
                                del self.loop_messages[slot_num]
                                if slot_num in self.loop_intervals: del self.loop_intervals[slot_num]
                                self.settings["loop_messages"] = self.loop_messages
                                self.settings["loop_intervals"] = self.loop_intervals
                                self.save_settings()
                                await self.highrise.chat(f"🗑️ <#00FF00>Permanent loop message slot {slot_num} has been cleared!")
                            else:
                                await self.highrise.chat(f"⚠️ No message found in slot {slot_num}")
                        else:
                            # Set the loop message
                            message_text = parts[2].strip()
                            self.loop_messages[slot_num] = message_text
                            self.settings["loop_messages"] = self.loop_messages
                            self.save_settings()
                            await self.highrise.chat(f"✅ <#00FF00>Loop message slot {slot_num} set to: <#FFFFFF>{message_text}")
                    except Exception as e:
                        print(f"Loop command error: {e}")
                        await self.highrise.chat("❌ Usage: !loop (number) (message) or !loop (number) to clear")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return



            elif msg_lower == "!startloop":
                if user.username in self.ADMINS:
                    if not self.loop_messages:
                        await self.highrise.chat("⚠️ No loop messages configured! Use !loop [number] [message] to add messages first.")
                        return
                    
                    self.loop_running = True
                    self.settings["loop_running"] = True
                    self.save_settings()
                    msg_count = len(self.loop_messages)
                    await self.highrise.chat(f"▶️ <#00FF00>Loop message system STARTED! ({msg_count} message{'s' if msg_count > 1 else ''} will repeat every {self.loop_cooldown}s)")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return

            elif msg_lower == "!stoploop":
                if user.username in self.ADMINS:
                    self.loop_running = False
                    self.settings["loop_running"] = False
                    self.save_settings()
                    await self.highrise.chat("⏸️ <#FF0000>Loop message system PAUSED!")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return

            elif msg_lower.startswith("!loopcooldown"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    if len(parts) < 2:
                        usage = f"ℹ️ Current Default Cooldown: {self.loop_cooldown}s\n"
                        usage += "Usage: !loopcooldown [seconds] OR !loopcooldown [slot] [seconds]"
                        await self.highrise.chat(usage)
                        return
                    
                    try:
                        if len(parts) == 2:
                            new_cooldown = int(parts[1])
                            if new_cooldown < 1:
                                await self.highrise.chat("⚠️ Please provide a positive number.")
                                return
                            self.loop_cooldown = new_cooldown
                            self.settings["loop_cooldown"] = new_cooldown
                            self.save_settings()
                            await self.highrise.chat(f"✅ <#00FF00>Default loop cooldown set to {new_cooldown}s!")
                        else:
                            slot_num = parts[1]
                            new_cooldown = int(parts[2])
                            if slot_num not in self.loop_messages:
                                await self.highrise.chat(f"❌ Slot {slot_num} not found.")
                                return
                            self.loop_intervals[slot_num] = new_cooldown
                            self.settings["loop_intervals"] = self.loop_intervals
                            self.save_settings()
                            await self.highrise.chat(f"✅ <#00FF00>Slot {slot_num} cooldown set to {new_cooldown}s!")
                    except ValueError:
                        await self.highrise.chat("❌ Invalid numbers provided.")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return

            elif msg_lower.startswith("!msgtime"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    if len(parts) < 2:
                        await self.highrise.chat("💡 Usage: !msgtime [slot_number] [seconds]\nExample: !msgtime 1 30")
                        return
                    
                    try:
                        # Handle !msgtime [seconds] (backward compatible)
                        if len(parts) == 2:
                            new_cooldown = int(parts[1])
                            if new_cooldown < 1:
                                await self.highrise.chat("⚠️ Please provide a positive number.")
                                return
                            self.loop_cooldown = new_cooldown
                            self.settings["loop_cooldown"] = new_cooldown
                            self.save_settings()
                            await self.highrise.chat(f"✅ <#00FF00>GLOBAL loop message time set to {new_cooldown}s!")
                        # Handle !msgtime [slot] [seconds]
                        elif len(parts) >= 3:
                            slot_num = parts[1]
                            new_cooldown = int(parts[2])
                            
                            if slot_num not in self.loop_messages:
                                await self.highrise.chat(f"❌ Slot {slot_num} has no message. Use !loop first.")
                                return
                                
                            if new_cooldown < 1:
                                await self.highrise.chat("⚠️ Please provide a positive number.")
                                return
                                
                            self.loop_intervals[slot_num] = new_cooldown
                            self.settings["loop_intervals"] = self.loop_intervals
                            self.save_settings()
                            await self.highrise.chat(f"✅ <#00FF00>Loop message slot {slot_num} time set to {new_cooldown}s!")
                    except ValueError:
                        await self.highrise.chat("❌ Please provide valid numbers. (Usage: !msgtime slot seconds)")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return

            elif msg_lower.startswith("!vipmessage"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    if len(parts) == 1:
                        status = "ON ✅" if self.vip_message_enabled else "OFF ❌"
                        await self.highrise.chat(f"👑 VIP Join Message is currently {status}\nUsage: !vipmessage [on/off]")
                        return
                    
                    subcmd = parts[1].lower()
                    if subcmd == "on":
                        self.vip_message_enabled = True
                        self.settings["vip_message_enabled"] = True
                        self.save_settings()
                        await self.highrise.chat(f"✅ <#00FF00>VIP Join Message is now ON! 👑")
                    elif subcmd == "off":
                        self.vip_message_enabled = False
                        self.settings["vip_message_enabled"] = False
                        self.save_settings()
                        await self.highrise.chat(f"❌ <#FF0000>VIP Join Message is now OFF!")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return


            elif msg_lower.startswith("!invite"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    
                    # Check for mentioned users
                    mentioned_users = [w[1:] for w in parts if w.startswith("@")]
                    
                    # Construct message (remove command and mentions)
                    msg_words = [w for w in parts[1:] if not w.startswith("@")]
                    invite_msg = " ".join(msg_words)
                    
                    if mentioned_users:
                        # Targeted Invite
                        await self.highrise.chat(f"🔍 Resolving {len(mentioned_users)} users...")
                        target_ids = []
                        
                        # Resolve Usernames to IDs
                        for u_name in mentioned_users:
                            try:
                                # Try to find in room first (faster)
                                r_users = (await self.highrise.get_room_users()).content
                                found = False
                                for ru, _ in r_users:
                                    if ru.username.lower() == u_name.lower():
                                        target_ids.append(ru.id)
                                        found = True
                                        break
                                
                                if not found:
                                    # Fallback to WebAPI
                                    resp = await self.webapi.get_users(username=u_name, limit=1)
                                    if resp.users:
                                        target_ids.append(resp.users[0].user_id)
                                    else:
                                        print(f"User {u_name} not found.")
                            except Exception as e:
                                print(f"Error resolving {u_name}: {e}")
                        
                        if target_ids:
                            try:
                                # Send Text Message First (if provided)
                                if invite_msg:
                                    await self.highrise.send_message_bulk(target_ids, invite_msg)
                                    await asyncio.sleep(0.5)
                                
                                # Send Invite Card
                                await self.highrise.send_message_bulk(target_ids, "Join our room!", "invite", self.room_id)
                                await self.highrise.chat(f"✅ Created invites for {len(target_ids)} users!")
                            except Exception as e:
                                await self.highrise.chat(f"❌ Error sending bulk invites: {e}")
                        else:
                            await self.highrise.chat("❌ No valid users found to invite.")
                        
                    else:
                        # Existing Mass Invite (All Conversations)
                        invite_msg = message[len("!invite"):].strip() # Get rest of message
                        await self.highrise.chat("📨 Sending invites to all recent conversations...")
                        
                        try:
                            conversations_resp = await self.highrise.get_conversations()
                            conversations = conversations_resp.conversations
                            
                            inv_pool = [
                                "<#00FFFF>🥳 Dosto room join karo party chal rahi hai! Jaldi aao! 🎉 🔥",
                                "<#FF69B4>🎵 We are playing the best tracks right now! Join our room for non-stop music and amazing vibes! 🎶✨",
                                "<#FFD700>👑 Upgrade to VIP to unlock premium perks, special teleports, and exclusive areas! Type !buyvip now! 💎✨",
                                "<#00FF00>🎮 Calling all gamers! Drop into our room for some epic high-level gameplay and chill sessions! 🕹️🏆",
                                "<#FF8C00>🎸 Live DJ! Let's vibe to the beats together. Hop in and request your favorite songs! 🎵🎧",
                                "<#8A2BE2>⭐ Become a VIP today and get access to exclusive Emotes & Mod features! 🌟👑",
                                "<#FF4500>🎲 Game night is on! Join us for fun mini-games, logic puzzles, and great company! 🎯🃏"
                            ]
                            count = 0
                            for conv in conversations:
                                try:
                                    current_msg = invite_msg if invite_msg else random.choice(inv_pool)
                                    await self.highrise.send_message(conv.id, current_msg)
                                    
                                    await self.highrise.send_message(conv.id, "Join our room!", "invite", self.room_id)
                                    count += 1
                                    await asyncio.sleep(1.2) # Throttled
                                except Exception as e:
                                    print(f"Failed to invite conv {conv.id}: {e}")
                                    
                            await self.highrise.chat(f"✅ Sent invites to {count} users!")
                            
                        except Exception as e:
                            print(f"Invite loop error: {e}")
                            await self.highrise.chat(f"❌ Error sending invites: {e}")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return
            elif msg_lower.startswith("!giveaway"):
                parts = message.split(" ", 2)
                
                # Check for subcommands
                if len(parts) > 1:
                    subcmd = parts[1].lower()
                    
                    # Public: Enter
                    if subcmd == "enter":
                        if not self.giveaway_info:
                            await self.highrise.chat("⚠️ No active giveaway to enter right now.")
                            return
                            
                        if user.username in self.giveaway_entries:
                            await self.highrise.chat(f"⚠️ @{user.username}, you have already entered this giveaway! ✅")
                        else:
                            self.giveaway_entries.append(user.username)
                            self.settings["giveaway_entries"] = self.giveaway_entries
                            self.save_settings()
                            count = len(self.giveaway_entries)
                            await self.highrise.chat(f"🎟️ @{user.username} successfully entered! (Total entries: {count}) 🍀")
                        return

                    # Admin Commands
                    if user.username in self.ADMINS:
                        if subcmd == "info":
                            if len(parts) > 2:
                                self.giveaway_info = parts[2].strip()
                                self.settings["giveaway_info"] = self.giveaway_info
                                self.save_settings()
                                await self.highrise.chat(f"📢 <#00FF00>Giveaway Info Updated: <#FFFFFF>{self.giveaway_info}")
                            else:
                                await self.highrise.chat("Usage: !giveaway info (message)")
                        
                        elif subcmd == "winner":
                            if not self.giveaway_entries:
                                await self.highrise.chat("⚠️ No entries in the giveaway to pick a winner from!")
                            else:
                                winner = random.choice(self.giveaway_entries)
                                await self.highrise.chat(f"🎉🎊 <#FFD700>AND THE WINNER IS... <#FFFFFF>@{winner}<#FFD700>! Congratulations! 🎊🎉")
                        
                        elif subcmd == "reset":
                            self.giveaway_info = ""
                            self.giveaway_entries = []
                            self.settings["giveaway_info"] = ""
                            self.settings["giveaway_entries"] = []
                            self.save_settings()
                            await self.highrise.chat("🗑️ <#FF0000>Giveaway has been RESET! Entries cleared.")
                        
                        else:
                            # Unknown subcommand, treated as public info request if invalid
                            pass
                    else:
                        # Non-admins trying admin commands or invalid subcommands
                        if subcmd in ["info", "winner", "reset"]:
                             await self.highrise.chat("❌ Admin only!")
                             return

                # Public: Display Info (Default)
                if not self.giveaway_info:
                    await self.highrise.chat("🔕 No active giveaway at the moment. Stay tuned!")
                else:
                    count = len(self.giveaway_entries)
                    msg = (
                        f"🎁 <#FFD700>CURRENT GIVEAWAY <#FFD700>🎁\n"
                        f"<#FFFFFF>{self.giveaway_info}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"👥 Entries: {count}\n"
                        f"👉 Type <#00FF00>!giveaway enter<#FFFFFF> to join!"
                    )
                    await self.highrise.chat(msg)
                return









            if msg_lower == "!sub":
                return

            elif msg_lower == "!unsub":
                initial_len = len(self.subscribers)
                self.subscribers = [s for s in self.subscribers if not (
                    (isinstance(s, dict) and s.get("id") == user.id) or 
                    (isinstance(s, str) and s.lower() == user.username.lower())
                )]
                
                if len(self.subscribers) < initial_len:
                    self.settings["subscribers"] = self.subscribers
                    self.save_settings()
                    await self.highrise.chat(f"🔒 <#FF0000>@{user.username} You have unsubscribed. Bot commands, emotes, and teleports are now LOCKED! ❌")
                else:
                    await self.highrise.chat(f"⚠️ <#FFFF00>@{user.username}, you are not subscribed.")
                return

            if msg_lower.startswith("!block "):
                if user.username in self.OWNERS or user.username in self.ADMINS:
                    try:
                        target = message.split(" ")[1].replace("@", "").strip()
                        # Owners cannot be blocked
                        if target in self.OWNERS:
                             await self.highrise.chat("❌ Cannot block an Owner.")
                             return

                        if target not in self.blocked_users:
                            self.blocked_users.append(target)
                            self.settings["blocked_users"] = self.blocked_users
                            self.save_settings()
                            await self.highrise.chat(f"🚫 <#FF0000>@{target} has been BLOCKED from using ALL commands!")
                        else:
                            await self.highrise.chat(f"⚠️ @{target} is already blocked.")
                    except:
                        await self.highrise.chat("<#FF0000>Usage: !block @username")
                else:
                    await self.highrise.chat("<#FF0000>❌ Admin/Owner only!")
                return

            elif msg_lower.startswith("!unblock "):
                if user.username in self.OWNERS or user.username in self.ADMINS:
                    try:
                        target = message.split(" ")[1].replace("@", "").strip()
                        if target in self.blocked_users:
                            self.blocked_users.remove(target)
                            self.settings["blocked_users"] = self.blocked_users
                            self.save_settings()
                            await self.highrise.chat(f"✅ <#00FF00>@{target} has been UNBLOCKED!")
                        else:
                            await self.highrise.chat(f"⚠️ @{target} is not blocked.")
                    except:
                        await self.highrise.chat("<#FF0000>Usage: !unblock @username")
                else:
                    await self.highrise.chat(trans["admins_only"])
                return



            if msg_lower.startswith("!help") or msg_lower.startswith("!मदद") or msg_lower.startswith("!ayuda") or msg_lower.startswith("!aide"):
                parts = message.split()
                category = parts[1].lower() if len(parts) > 1 else ""

                if not category:
                    await self.safe_chat(trans.get("m_intro", "Type !help public, !help fun, !help vip..."))
                
                elif category == "emotelist":
                    if not self.emotes:
                        await self.safe_chat("❌ No emotes configured.")
                    else:
                        await self.safe_chat(f"🎭 Sending {len(self.emote_list)} emotes...")
                        chunk_size = 20
                        for i in range(0, len(self.emote_list), chunk_size):
                            chunk = self.emote_list[i : i + chunk_size]
                            lines = []
                            for j, name in enumerate(chunk, i + 1):
                                lines.append(f"{j}. {name}")
                            await self.safe_chat(f"🎭 Emotes {i+1}-{i+len(chunk)}:\n" + "\n".join(lines))
                            await asyncio.sleep(1.0)

                elif category == "public":
                    await self.safe_chat(trans.get("m_pub", (
                        "<#FF00FF>🌟 PUBLIC COMMANDS 🌟\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!sub / !unsub <#800080>- Lock/Unlock Bot\n"
                        "<#FFFF00>!ping / !uptime <#800080>- Status\n"
                        "<#FFFF00>!user <#800080>- Who is in room?\n"
                        "<#FFFF00>!profile <#800080>- Your stats\n"
                        "<#FFFF00>!emotelist <#800080>- Emote Catalog\n"
                        "<#FFFF00>!level / !xp <#800080>- Check Level\n"
                        "<#FFFF00>!lb / !lb2 <#800080>- Leaderboards\n"
                        "<#FFFF00>!flash [on/off] <#800080>- Instant TP Mode\n"
                    )))

                elif category == "fun":
                    await self.safe_chat(trans.get("m_fun", (
                        "<#FF00FF>🎭 FUN COMMANDS 🎭\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!rizz <#800080>- Pickup lines\n"
                        "<#FFFF00>!roast @user <#800080>- Roast response\n"
                        "<#FFFF00>!flirt <#800080>- Flirty message\n"
                        "<#FFFF00>!joke <#800080>- Tell a joke\n"
                        "<#FFFF00>!shayari <#800080>- Poetry\n"
                        "<#FFFF00>!love / !hate @user\n"
                        "<#FFFF00>!deathyear @user"
                    )))

                elif category == "vip":
                    await self.safe_chat(trans.get("m_vip", (
                        "<#FF00FF>👑 VIP COMMANDS 👑\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!vipcost <#800080>- Price Check\n"
                        "<#FFFF00>!viplist <#800080>- Member List\n"
                        "<#FFFF00>!buyvip <#800080>- Buy VIP (Tip first)"
                    )))

                elif category in ["teleports", "tele"]:
                    await self.safe_chat(trans.get("m_tele", (
                        "<#FF00FF>📍 TELEPORT COMMANDS 📍\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!telelist <#800080>- List all Locations\n"
                        "<#FFFF00>!create tele [name]\n"
                        "<#FFFF00>!remtele [name]\n"
                        "<#FFFF00>!cleartele\n"
                        "<#FFFF00>[name] <#800080>- Type name to flash!"
                    )))

                elif category == "giveaway":
                    await self.safe_chat(trans.get("m_give", (
                        "<#FF00FF>🎁 GIVEAWAY COMMANDS 🎁\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!giveaway <#800080>- Show Status\n"
                        "<#FFFF00>!giveaway enter <#800080>- Join In\n"
                        "<#FFFF00>!giveaway info [msg] <#800080>- (Admin)\n"
                        "<#FFFF00>!giveaway winner <#800080>- (Admin)"
                    )))

                elif category == "reaction":
                    await self.safe_chat(trans.get("m_react", (
                        "<#FF00FF>❤️ REACTION COMMANDS ❤️\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!heartall / !winkall / !thumbsall / !waveall"
                    )))

                elif category in ["botoutfit", "outfit", "fit"]:
                    await self.safe_chat(trans.get("m_fit", (
                        "<#FF00FF>👔 BOT OUTFIT SYSTEM 👔\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                        "<#FFFF00>!getfit @user / !savefit [name]\n"
                        "<#FFFF00>!loadfit [name] / !fitlist"
                    )))

                elif category == "moderator" or category == "mod":
                    if user.username not in self.ADMINS:
                        await self.safe_chat(trans.get("admins_only", "❌ Admin only!"))
                    else:
                        await self.safe_chat(trans.get("m_mod", (
                            "<#FF00FF>🛡️ MODERATOR COMMANDS 🛡️\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>!kick / !ban / !mute / !freeze\n"
                            "<#FFFF00>!tip / !tipall\n"
                            "<#FFFF00>!setjoin [msg] / !history"
                        )))

                elif category == "owner":
                    if user.username not in self.OWNERS:
                        await self.safe_chat(trans.get("owners_only", "❌ Owner only!"))
                    else:
                        await self.safe_chat("⚡ **OWNER CONTROLS** ⚡\n!addadmin, !remadmin, !owner, !remowner, !block, !cashout, !restartbot, !clear data, !uptime")
                
                else:
                     await self.safe_chat("❌ Unknown category. Type !help for the menu.")

            elif msg_lower == "!user":
                try:
                    room_users = (await self.highrise.get_room_users()).content
                    user_list = [f"@{u.username}" for u, _ in room_users]
                    count = len(user_list)
                    users_str = ", ".join(user_list)
                    await self.safe_chat(f"<#00FFFF>👥 Users in Room ({count}):\n<#FF69B4>{users_str}")
                except Exception as e:
                    print(f"Error getting users: {e}")
                    await self.highrise.chat("<#FF0000>Error fetching user list.")

            elif msg_lower.startswith("!profile"):
                try:
                    target_username = user.username
                    parts = message.split()
                    if len(parts) > 1:
                        target_username = parts[1].replace("@", "")
                    
                    # Data resolution
                    stats = self.user_stats.get(target_username, {})
                    joined_date = stats.get("joined_bot_date", stats.get("first_seen", "Not Subscribed ❌"))
                    
                    # Check online status for accurate last seen
                    r_users = (await self.highrise.get_room_users()).content
                    is_online = any(u.username.lower() == target_username.lower() for u, _ in r_users)
                    last_seen = stats.get("last_seen", "Online Now 🟢") if not is_online else "Online Now 🟢"
                    
                    is_banned = "Yes 🚫" if target_username.lower() in [b.lower() for b in self.banned_users] else "No ✅"
                    
                    # VIP Info
                    is_vip_active = False
                    expiry_str = "N/A"
                    if isinstance(self.VIPS, dict) and target_username in self.VIPS:
                        exp = self.VIPS[target_username]
                        if exp == "permanent":
                            is_vip_active = True
                            expiry_str = "Permanent ♾️"
                        elif isinstance(exp, (int, float)):
                            if exp > time.time():
                                is_vip_active = True
                                date_obj = datetime.fromtimestamp(exp)
                                expiry_str = date_obj.strftime("%Y-%m-%d")
                            else:
                                expiry_str = "Expired"
                    
                    vip_history = self.settings.get("vip_history", [])
                    is_vip_status = "Yes ✅" if is_vip_active else "No ❌"
                    is_activated = "Active 🟢" if is_vip_active else "Inactive 🔴"
                    
                    # Rank & Activity
                    cmds_used = self.command_usage.get(target_username, 0)
                    msg_count = self.chat_stats.get(target_username, 0)
                    total_secs = self.user_times.get(target_username, 0)
                    
                    # Current session addition
                    # Search for target in join_times
                    room_users = (await self.highrise.get_room_users()).content
                    for u, _ in room_users:
                        if u.username.lower() == target_username.lower():
                            if u.id in self.join_times:
                                total_secs += int(time.time() - self.join_times[u.id])
                            break

                    hrs = total_secs // 3600
                    mins = (total_secs % 3600) // 60
                    score_pts = (total_secs // 60) + (msg_count * 2)
                    activity_score = f"{score_pts} Points 🌟 ({hrs}h {mins}m | {msg_count} msgs)"
                    
                    rank = "🌱 Newcomer"
                    if total_secs > 36000: rank = "🏆 Legend"
                    elif total_secs > 18000: rank = "🎖️ Veteran"
                    elif total_secs > 3600: rank = "👤 Regular"
                    elif cmds_used > 50: rank = "☘️ Active"
                    
                    # Format Profile Card
                    profile_card = (
                        f"👤 Profile of @{target_username}\n"
                        f"--------------------------------\n"
                        f"🗓️ Joined Date: {joined_date}\n"
                        f"🌐 Language: {self.language.title()}\n"
                        f"🛡️ Banned: {is_banned}\n"
                        f"🕒 Last Seen: {last_seen}\n\n"
                        f"🌟 VIP Status\n"
                        f"  - Is VIP: {is_vip_status}\n"
                        f"  - Activated: {is_activated}\n"
                        f"  - Expiry: {expiry_str}\n\n"
                        f"🏆 Rank Info\n"
                        f"  - Rank: {rank}\n"
                        f"  - Commands Used: {cmds_used}\n"
                        f"  - Activity Score: {activity_score}\n"
                        f"--------------------------------"
                    )
                    
                    # Deliver via Inbox (Direct Message) as requested
                    # This uses the bot's messaging system to send a personal profile message
                    try:
                        # use send_message_bulk which is the recommended way to send to room users directly
                        await self.highrise.send_message_bulk([user.id], profile_card)
                        return
                    except Exception as e:
                        print(f"DM fallback: {e}")
                        # Final fallback to public chat if inbox fails
                        await self.safe_chat(profile_card)
                except Exception as e:
                    print(f"Profile error in chat: {e}")
                    await self.highrise.chat("❌ Error fetching profile card.")

            elif msg_lower == "!updates":
                changelog = (
                    "<#00FFFF>🚀 Bot Updates & Changelog 🚀\n\n"
                    "<#FFFF00>✅ Moderation:\n"
                    "<#FFFFFF>• Added !ban and !unban commands.\n"
                    "<#FFFFFF>• Added !kick moderation.\n\n"
                    "<#FFFF00>✅ Wallet & Tipping:\n"
                    "<#FFFFFF>• Added !tip and !tipall for admins.\n"
                    "<#FFFFFF>• Added !wallet to check bot balance.\n\n"
                    "<#FFFF00>✅ Maintenance:\n"
                    "<#FFFFFF>• Added !restartbot for quick reboots.\n"
                    "<#FFFFFF>• Added !chaton/off tracking system.\n"
                    "<#FFFFFF>• Removed AI for better speed.\n\n"
                    "<#FFFF00>✅ Data Reset:\n"
                    "<#FFFFFF>• !clear data now wipes EVERYTHING (VIPs, Admins, and ALL Teleports)."
                )
                await self.safe_chat(changelog)




            elif message.startswith("!rolelist"):
                if user.username in self.OWNERS:
                    # Owners
                    if self.OWNERS:
                        owners_list = ", ".join([f"@{o}" for o in self.OWNERS])
                    else: owners_list = "None"
                    
                    # Admins
                    if self.ADMINS:
                        admins_list = ", ".join([f"@{a}" for a in self.ADMINS])
                    else: admins_list = "None"
                    
                    msg = (
                        f"📜 **ROLE LIST** 📜\n"
                        f"👑 Owners: {owners_list}\n"
                        f"🛡️ Admins: {admins_list}"
                    )
                    await self.safe_chat(msg)
                else:
                    await self.highrise.chat("❌ Owner only!")
                return

            elif msg_lower == "!history":
                if user.username in self.ADMINS or user.username in self.OWNERS:
                    if not self.tip_history:
                        await self.highrise.chat("📜 No tip history found yet.")
                    else:
                        history_str = "📜 <#FFD700>RECENT 10 TIPS HISTORY 📜\n"
                        # Take last 10
                        recent_10 = self.tip_history[-10:]
                        recent_10.reverse() # Show newest first
                        for i, entry in enumerate(recent_10, 1):
                            history_str += f"{i}. @{entry['username']} tipped {entry['amount']}G\n"
                        await self.safe_chat(history_str)
                else:
                    await self.highrise.chat(f"@{user.username} ❌ This command is restricted to Moderators and Owners only!")

            elif msg_lower == "!lb":
                # Time Leaderboard
                # Combine saved time with current session time for online users
                full_times = self.user_times.copy()
                for uid, start in self.join_times.items():
                    # Need to resolve username for the leaderboard
                    # We can try to get it from room users if they are online
                    pass # We'll just use what's in self.user_times for simplicity or match IDs if possible
                
                sorted_lb = sorted(self.user_times.items(), key=lambda x: x[1], reverse=True)[:10]
                if not sorted_lb:
                    await self.highrise.chat("⏳ Time leaderboard is empty!")
                else:
                    lb_str = "⏳ <#00FFFF>TOP 10 TIME LEADERS ⏳\n"
                    for i, (name, secs) in enumerate(sorted_lb, 1):
                        hrs = secs // 3600
                        mins = (secs % 3600) // 60
                        lb_str += f"{i}. @{name} - {hrs}h {mins}m\n"
                    await self.safe_chat(lb_str)

            elif msg_lower == "!lb2":
                # Chat Leaderboard
                sorted_chat = sorted(self.chat_stats.items(), key=lambda x: x[1], reverse=True)[:10]
                if not sorted_chat:
                    await self.highrise.chat("💬 Chat leaderboard is empty!")
                else:
                    lb_str = "💬 <#00FF00>TOP 10 CHAT LEADERS 💬\n"
                    for i, (name, count) in enumerate(sorted_chat, 1):
                        lb_str += f"{i}. @{name} - {count} msgs\n"
                    await self.safe_chat(lb_str)

            elif msg_lower == "!mytime":
                secs = self.user_times.get(user.username, 0)
                # Add current session if online
                if user.id in self.join_times:
                    secs += int(time.time() - self.join_times[user.id])
                
                hrs = secs // 3600
                mins = (secs % 3600) // 60
                await self.highrise.chat(f"🕒 @{user.username}, your total time in room: {hrs}h {mins}m {secs%60}s")

            elif message.startswith("!time "):
                try:
                    target = message.split(" ")[1].replace("@", "").strip()
                    secs = self.user_times.get(target, 0)
                    # Check if online for current session
                    room_users = (await self.highrise.get_room_users()).content
                    for u, _ in room_users:
                        if u.username.lower() == target.lower():
                            if u.id in self.join_times:
                                secs += int(time.time() - self.join_times[u.id])
                            break
                    
                    hrs = secs // 3600
                    mins = (secs % 3600) // 60
                    await self.highrise.chat(f"🕒 @{target} has spent {hrs}h {mins}m {secs%60}s in this room.")
                except:
                    await self.highrise.chat("Usage: !time @username")

            elif msg_lower == "!cleartime":
                if user.username in self.ADMINS:
                    self.user_times = {}
                    self.settings["user_times"] = {}
                    self.save_settings()
                    await self.highrise.chat("🗑️ Time leaderboard has been cleared by Admin!")
                else:
                    await self.highrise.chat("❌ Admin only!")

            elif msg_lower == "!clearchat":
                if user.username in self.ADMINS:
                    self.chat_stats = {}
                    self.settings["chat_stats"] = {}
                    self.save_settings()
                    await self.highrise.chat("🗑️ Chat leaderboard has been cleared by Admin!")
                else:
                    await self.highrise.chat("❌ Admin only!")



            elif msg_lower == "!stats":
                target_user = user.username
                
                # 1. Time Spent
                secs = self.user_times.get(target_user, 0)
                if user.id in self.join_times:
                    secs += int(time.time() - self.join_times[user.id])
                hours = secs // 3600
                mins = (secs % 3600) // 60
                
                # 2. Messages & Stats
                msgs = self.chat_stats.get(target_user, 0)
                cmds = self.command_usage.get(target_user, 0)
                
                # 3. Level Info
                level_info = ""
                if target_user in self.user_levels:
                    lvl = self.user_levels[target_user]["level"]
                    level_info = f" | 🌟 Lvl {lvl}"
                
                msg = (
                    f"📊 <#00FFFF>Stats for @{target_user}{level_info}\n"
                    f"<#FFFFFF>🕒 Time: <#FFFF00>{hours}h {mins}m\n"
                    f"<#FFFFFF>💬 Msgs: <#FFFF00>{msgs}\n"
                    f"<#FFFFFF>🤖 Cmds: <#FFFF00>{cmds}"
                )
                await self.highrise.chat(msg)


            elif msg_lower.startswith("!lovepercent"):
                parts = message.split()
                if len(parts) < 2:
                    await self.highrise.chat("❌ Usage: !lovepercent @username")
                    return
                
                target_user = parts[1].replace("@", "")
                
                if target_user.lower() == user.username.lower():
                     await self.highrise.chat(f"❤️ @{user.username}, you gotta love yourself first! 100% self-love! ❤️")
                     return

                percentage = random.randint(0, 100)
                
                comment = ""
                if percentage <= 20:
                     comment = "💔 It's barely platonic..."
                elif percentage <= 50:
                     comment = "🤔 Just friends?"
                elif percentage <= 80:
                     comment = "❤️ There's a spark!"
                else:
                     comment = "🔥 IT'S TRUE LOVE! 🔥"
                
                await self.highrise.chat(f"💘 Love Meter: @{user.username} ❤️ @{target_user} = {percentage}%\n{comment}")


            elif msg_lower == "!ping":
                start_time = time.time()
                await self.highrise.chat("Pinging... 🕒")
                end_time = time.time()
                latency = round((end_time - start_time) * 1000)
                ping_text = self.translations.get(self.language, self.translations["english"])["ping"]
                await self.highrise.chat(ping_text.format(latency=latency))

            elif msg_lower == "!uptime":
                if user.username in self.OWNERS:
                    uptime_seconds = int(time.time() - self.start_time)
                    days = uptime_seconds // 86400
                    hours = (uptime_seconds % 86400) // 3600
                    minutes = (uptime_seconds % 3600) // 60
                    
                    start_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.start_time))
                    uptime_msg = (
                        f"🚀 **Bot Online Status** 🚀\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🕒 **Uptime:** {days}d {hours}h {minutes}m\n"
                        f"📅 **Started On:** {start_date}\n"
                        f"━━━━━━━━━━━━━━━━━━"
                    )
                    await self.safe_chat(uptime_msg)
                else:
                    await self.highrise.chat("❌ This command is restricted to Bot Owners only!")





            elif msg_lower in ["!stop all", "stop all"]:
                if user.username in self.ADMINS:
                    count = len(self.looping_users)
                    # Stop everyone's current animation immediately
                    for u_id in list(self.looping_users.keys()):
                        try:
                            await self.highrise.send_emote("idle-dance-casual", u_id)
                        except: pass
                    self.looping_users.clear()
                    await self.highrise.chat(f"🛑 <#FF0000>EMOTE STOP! <#FFFFFF>Cleared loops for {count} users.")
                else:
                    await self.highrise.chat(self.translations.get(self.language, self.translations["english"])["admins_only"])

            elif msg_lower in ["!stop", "stop", "stop emote", "!stop emote"] or (msg_lower.startswith("!stop ") and "@" in msg_lower):
                parts = message.split()
                if msg_lower.startswith("!stop") and len(parts) > 1 and parts[1].startswith("@"):
                    # Admin Command: !stop @username
                    if user.username in self.ADMINS:
                        target_name = parts[1][1:]
                        room_users = (await self.highrise.get_room_users()).content
                        target_id = None
                        for u, _ in room_users:
                            if u.username.lower() == target_name.lower():
                                target_id = u.id
                                break
                        
                        if target_id:
                            if target_id in self.looping_users:
                                self.looping_users.pop(target_id, None)
                                await self.highrise.chat(f"🛑 Stopped emote loop for @{target_name}!")
                            else:
                                await self.highrise.chat(f"ℹ️ @{target_name} is not currently looping.")
                        else:
                             await self.highrise.chat(f"❌ User @{target_name} not found.")
                    else:
                        await self.highrise.chat(self.translations.get(self.language, self.translations["english"])["admins_only"])
                else:
                    self.looping_users.pop(user.id, None) # Remove from loop
                    try:
                        await self.highrise.send_emote("idle-dance-casual", user.id)
                    except: pass
                    await self.highrise.chat(f"@{user.username} Stopped emote! 🛑")

            elif msg_lower in ["!emotelist", "emotelist"]:
                if not self.emote_list:
                    await self.highrise.chat("<#FF0000>No emotes configured.")
                else:
                    await self.highrise.chat(f"🎭 Sending {len(self.emote_list)} emotes! Use names or numbers. 💃")
                    
                    # Create numbered list in smaller chunks (Highrise limit is 256 chars)
                    chunk_size = 6
                    for i in range(0, len(self.emote_list), chunk_size):
                        chunk = self.emote_list[i : i + chunk_size]
                        lines = [f"📜 **EMOTES {i+1}-{i+len(chunk)}**"]
                        for j, name in enumerate(chunk, i + 1):
                            lines.append(f"{j}. {name.title()}")
                        
                        full_chunk = "\n".join(lines)
                        await self.highrise.chat(full_chunk)
                        await asyncio.sleep(1.2)
                    
                    await self.highrise.chat("💡 Tip: Type !number or just the emote name (e.g. ghostfloat)!")


            elif msg_lower == "!come":
                if user.username in self.ADMINS:
                    dest_pos = await get_user_position(self, user.id)
                    if dest_pos:
                        await self.highrise.chat("arha hu meri jaan thoda sa sabar kar")
                        await self.highrise.walk_to(dest_pos)
                    else:
                        await self.highrise.chat("<#FF0000>Could not find your position!")

            elif msg_lower == "!set":
                if user.username in self.ADMINS:
                    room_users = (await self.highrise.get_room_users()).content
                    for target, pos in room_users:
                        if target.id == user.id and isinstance(pos, Position):
                            self.settings["perm_pos"] = {"x": pos.x, "y": pos.y, "z": pos.z, "facing": pos.facing}
                            self.save_settings()
                            await self.highrise.chat("Position set!")
                            break

            elif msg_lower in ("!مكانك", "مكانك", "!موقفك", "موقفك"):
                if user.username in self.OWNERS or user.username in self.ADMINS:
                    room_users = (await self.highrise.get_room_users()).content
                    found = False
                    for target, pos in room_users:
                        if target.id == user.id and isinstance(pos, Position):
                            self.settings["perm_pos"] = {"x": pos.x, "y": pos.y, "z": pos.z, "facing": pos.facing}
                            self.save_settings()
                            # Move bot to this position immediately
                            try:
                                await self.highrise.walk_to(Position(pos.x, pos.y, pos.z, pos.facing))
                            except Exception:
                                pass
                            await self.highrise.chat(
                                f"📍 تم حفظ موقعك كنقطة الاتصال الدائمة للبوت!\n"
                                f"({pos.x:.1f}, {pos.y:.1f}, {pos.z:.1f})"
                            )
                            found = True
                            break
                    if not found:
                        await self.highrise.chat("❌ مش قادر أجيب موقعك، جرب تاني")
                else:
                    await self.highrise.chat("❌ الأونر والأدمن بس يقدروا يحفظوا الموقع")

            elif msg_lower.startswith("!setjoin"):
                if user.username in self.ADMINS:
                    parts = message.split(" ", 1)
                    if len(parts) < 2:
                        await self.highrise.chat("Usage: !setjoin [message] \nPlaceholders: @username, {visits}, {room}, {date}")
                        return
                    
                    new_msg = parts[1].strip()
                    self.settings["custom_welcome_message"] = new_msg
                    self.save_settings()
                    
                    # Preview placeholders
                    preview = new_msg.replace('@username', f"@{user.username}")
                    preview = preview.replace('{username}', user.username)
                    preview = preview.replace('{room}', self.current_room_name)
                    preview = preview.replace('{date}', time.strftime("%Y-%m-%d"))
                    preview = preview.replace('{visits}', "1")
                    
                    await self.highrise.chat(f"✅ Welcome message updated!\nPreview: {preview}")
                else:
                    await self.highrise.chat("❌ Admin only!")
                return



            elif msg_lower.startswith("!adminmessage"):
                if user.username in self.OWNERS:
                    parts = message.split()
                    if len(parts) < 2:
                        await self.highrise.chat("Usage: !adminmessage [on/off]")
                        return
                    toggle = parts[1].lower()
                    if toggle == "on":
                        self.admin_message_enabled = True
                        self.settings["admin_message_enabled"] = True
                        self.save_settings()
                        await self.highrise.chat("✅ Admin join messages ENABLED!")
                    elif toggle == "off":
                        self.admin_message_enabled = False
                        self.settings["admin_message_enabled"] = False
                        self.save_settings()
                        await self.highrise.chat("❌ Admin join messages DISABLED!")
                else:
                    await self.highrise.chat("❌ Owner only!")
                return

            elif msg_lower.startswith("!ownermessage"):
                if user.username in self.OWNERS:
                    parts = message.split()
                    if len(parts) < 2:
                        await self.highrise.chat("Usage: !ownermessage [on/off]")
                        return
                    toggle = parts[1].lower()
                    if toggle == "on":
                        self.owner_message_enabled = True
                        self.settings["owner_message_enabled"] = True
                        self.save_settings()
                        await self.highrise.chat("✅ Owner join messages ENABLED!")
                    elif toggle == "off":
                        self.owner_message_enabled = False
                        self.settings["owner_message_enabled"] = False
                        self.save_settings()
                        await self.highrise.chat("❌ Owner join messages DISABLED!")
                else:
                    await self.highrise.chat("❌ Owner only!")
                return


            elif msg_lower == "!back":
                if "perm_pos" in self.settings:
                    p = self.settings["perm_pos"]
                    await self.highrise.walk_to(Position(p["x"], p["y"], p["z"], p.get("facing", "FrontRight")))

            # ==================== RADIO COMMANDS ====================
            elif msg_lower.startswith("!p ") and len(msg_lower) > 3:
                query = message[3:].strip()
                if not query:
                    await self.highrise.chat("<#FF4500>🎵 استخدم: !p اسم الأغنية")
                else:
                    _admins_lower = [a.lower() for a in self.ADMINS + self.OWNERS + self.hardcoded_owners] + list(self.room_moderators)
                    is_privileged = user.username.lower() in _admins_lower
                    if not is_privileged:
                        tickets = self.radio_tickets.get(user.username, 3)
                        if tickets <= 0:
                            await self.highrise.chat(f"<#FF0000>🎫 مفيش تيكتس @{user.username}!\nاطلب من الأدمن يضيف تيكتس بـ !addtickets")
                            return
                    await self.highrise.chat(f"<#FFD700>🔍 بدور على: {query}...")
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                f"{self.RADIO_API_URL}/api/radio/request",
                                json={"query": query, "requestedBy": user.username},
                                timeout=aiohttp.ClientTimeout(total=45)
                            ) as resp:
                                data = await resp.json()
                        if resp.status == 200 and data.get("success"):
                            song = data["song"]
                            if not is_privileged:
                                self.radio_tickets[user.username] = self.radio_tickets.get(user.username, 3) - 1
                                self.settings["radio_tickets"] = self.radio_tickets
                                self.save_settings()
                            try:
                                radio_playlists.add_song(song["title"], song["url"], song.get("uploader",""), song.get("duration",0))
                            except Exception:
                                pass
                            tickets_left = self.radio_tickets.get(user.username, 3) if not is_privileged else "∞"
                            if data.get("isQueued"):
                                await self.highrise.chat(
                                    f"<#00CED1>✅ اتضافت للقائمة!\n"
                                    f"<#FFD700>{song['title'][:45]}\n"
                                    f"<#C0C0C0>رقم {data.get('queuePosition','?')} | تيكتس: {tickets_left}"
                                )
                            else:
                                dur = song.get("duration", 0)
                                mins, secs = int(dur) // 60, int(dur) % 60
                                await self.highrise.chat(
                                    f"<#FF4500>🎵 الآن يعزف!\n"
                                    f"<#FFD700>{song['title'][:40]}\n"
                                    f"<#00CED1>{song.get('uploader','')[:25]}\n"
                                    f"<#FFFFFF>⏱ {mins}:{secs:02d} | تيكتس: {tickets_left}"
                                )
                        else:
                            err = data.get("error", "خطأ غير معروف")
                            await self.highrise.chat(f"<#FF0000>❌ {err}")
                    except Exception as e:
                        err_msg = str(e)
                        if "too long" in err_msg.lower() or "Message too long" in err_msg:
                            pass
                        else:
                            await self.highrise.chat(f"<#FF0000>❌ مش لاقيش '{query}'. جرب اسم تاني!")
                        print(f"[Radio] !p error: {e}")

            elif msg_lower == "!s":
                try:
                    _admins_lower = [a.lower() for a in self.ADMINS + self.OWNERS + self.hardcoded_owners] + list(self.room_moderators)
                    is_privileged = user.username.lower() in _admins_lower

                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{self.RADIO_API_URL}/api/radio/current",
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            cur_data = await resp.json()
                    current = cur_data.get("song") if cur_data.get("playing") else None
                    if current and not is_privileged:
                        requester = current.get("requestedBy", "")
                        if requester and requester != user.username:
                            await self.highrise.chat(
                                f"<#FF0000>⛔ الأغنية دي طلبها @{requester}\n"
                                f"<#C0C0C0>مش ليك حق تعمل سكيب 🎵"
                            )
                            return
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{self.RADIO_API_URL}/api/radio/skip",
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            data = await resp.json()
                    if data.get("success"):
                        skipped = data.get("skipped")
                        nxt = data.get("next")
                        msg = "<#FF4500>⏭ تم تخطي الأغنية!"
                        if skipped:
                            msg += f"\n<#C0C0C0>{skipped['title'][:35]}"
                        if nxt:
                            msg += f"\n<#FFD700>الجاي: {nxt['title'][:35]}"
                        await self.highrise.chat(msg)
                    else:
                        await self.highrise.chat("<#C0C0C0>📻 الراديو مش شغال دلوقتي")
                except Exception as e:
                    await self.highrise.chat("<#C0C0C0>📻 مش قادر أتواصل مع الراديو")
                    print(f"[Radio] !s error: {e}")

            elif msg_lower == "!q":
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{self.RADIO_API_URL}/api/radio/queue",
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            data = await resp.json()
                    current = data.get("currentSong")
                    queue = data.get("queue", [])
                    if not data.get("isPlaying") and not current:
                        await self.highrise.chat(
                            f"<#C0C0C0>📻 الراديو واقف\n"
                            f"<#FFD700>اضيف أغاني بـ !p اسم الأغنية"
                        )
                    else:
                        req_by = current.get("requestedBy", "") if current else ""
                        lines = [f"<#FF4500>🎵 يعزف: {current['title'][:40] if current else '---'}"]
                        if req_by:
                            lines.append(f"<#C0C0C0>طلب: @{req_by}")
                        for i, s in enumerate(queue[:4], 1):
                            lines.append(f"<#FFD700>{i}. {s['title'][:35]}")
                        if len(queue) > 4:
                            lines.append(f"<#C0C0C0>+{len(queue)-4} أغاني")
                        await self.highrise.chat("\n".join(lines))
                except Exception as e:
                    await self.highrise.chat(f"<#C0C0C0>📻 مش قادر أجيب القائمة")
                    print(f"[Radio] !q error: {e}")

            elif msg_lower in ("!mytickets", "!تذاكري", "!تذكرتي", "!رصيدي"):
                tickets = self.radio_tickets.get(user.username, 3)
                _admins_lower = [a.lower() for a in self.ADMINS + self.OWNERS + self.hardcoded_owners]
                is_privileged = user.username.lower() in _admins_lower
                if is_privileged:
                    await self.highrise.chat(f"<#FFD700>🎫 @{user.username} تيكتساتك: ∞ (أدمن)")
                else:
                    await self.highrise.chat(f"<#FFD700>🎫 @{user.username} تيكتساتك: {tickets} 🎟")

            elif msg_lower.startswith("!addtickets ") or msg_lower.startswith("!addticket "):
                _admins_lower = [a.lower() for a in self.ADMINS + self.OWNERS + self.hardcoded_owners] + list(self.room_moderators)
                if user.username.lower() in _admins_lower:
                    parts = msg_lower.split()
                    if len(parts) >= 3:
                        target = parts[1].replace("@", "")
                        try:
                            amount = int(parts[2])
                            self.radio_tickets[target] = self.radio_tickets.get(target, 3) + amount
                            self.settings["radio_tickets"] = self.radio_tickets
                            self.save_settings()
                            await self.highrise.chat(f"<#00FF00>✅ اتضافوا {amount} تيكت لـ @{target} | الإجمالي: {self.radio_tickets[target]}")
                        except ValueError:
                            await self.highrise.chat("<#FF0000>❌ اكتب رقم صحيح")
                    else:
                        await self.highrise.chat("<#FF0000>❌ استخدم: !addtickets @user عدد")
                else:
                    await self.highrise.chat("<#FF0000>❌ أدمن بس!")

            elif msg_lower == "!list" or msg_lower == "!list r":
                genres = radio_playlists.list_genres()
                if not genres:
                    await self.highrise.chat("<#C0C0C0>📂 مفيش بلايليست لسه\nاطلب أغاني بـ !p وهتتحفظ تلقائياً")
                else:
                    lines = ["<#FF4500>📂 البلايليست:"]
                    for genre, count in genres.items():
                        name = radio_playlists.genre_display_name(genre)
                        lines.append(f"<#FFD700>{name} ({count}) - !list {genre}")
                    lines.append("<#C0C0C0>!mex ميكس عشوائي من الكل")
                    await self.highrise.chat("\n".join(lines[:8]))

            elif msg_lower.startswith("!list ") and len(msg_lower) > 6:
                genre = msg_lower[6:].strip()
                songs = radio_playlists.get_songs(genre)
                if not songs:
                    available = list(radio_playlists.list_genres().keys())
                    await self.highrise.chat(f"<#FF0000>❌ مفيش بلايليست '{genre}'\nالمتاح: {', '.join(available) if available else 'لا يوجد'}")
                else:
                    genre_name = radio_playlists.genre_display_name(genre)
                    lines = [f"<#FF4500>📂 {genre_name} ({len(songs)} أغنية):"]
                    for i, s in enumerate(songs[:10], 1):
                        dur = s.get("duration", 0)
                        mins = int(dur) // 60
                        secs = int(dur) % 60
                        lines.append(f"<#FFD700>{i}. {s['title'][:38]}")
                        lines.append(f"<#C0C0C0>   ⏱{mins}:{secs:02d}")
                    if len(songs) > 10:
                        lines.append(f"<#C0C0C0>...و {len(songs)-10} أغاني أخرى")
                    lines.append(f"<#00CED1>━━━━━━━━━━━━━━━")
                    lines.append(f"<#00FF00>اكتب yes لتشغيلهم | no للإلغاء")
                    self.pending_playlist[user.username] = {"genre": genre, "songs": songs}
                    await self.highrise.chat("\n".join(lines[:15]))

            elif msg_lower in ("!yes", "!اه", "!نعم", "!ايوه"):
                pending = self.pending_playlist.get(user.username)
                if not pending:
                    await self.highrise.chat(f"<#C0C0C0>@{user.username} مفيش طلب بلايليست منتظر")
                else:
                    genre = pending["genre"]
                    songs = pending["songs"]
                    del self.pending_playlist[user.username]
                    genre_name = radio_playlists.genre_display_name(genre)
                    await self.highrise.chat(f"<#00CED1>🎵 بيضيف {min(len(songs),20)} أغنية من {genre_name}...")
                    added = 0
                    for s in songs[:20]:
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.post(
                                    f"{self.RADIO_API_URL}/api/radio/add",
                                    json={"title": s["title"], "url": s["url"], "uploader": s.get("uploader",""), "duration": s.get("duration",0), "requestedBy": f"playlist:{genre}"},
                                    timeout=aiohttp.ClientTimeout(total=5)
                                ) as resp:
                                    if resp.status == 200:
                                        added += 1
                        except Exception:
                            pass
                    await self.highrise.chat(f"<#00FF00>✅ اتضافوا {added} أغنية من {genre_name} للقائمة! 🎶")

            elif msg_lower in ("!no", "!la", "!لا", "!الغاء", "!إلغاء"):
                if user.username in self.pending_playlist:
                    genre = self.pending_playlist[user.username]["genre"]
                    del self.pending_playlist[user.username]
                    await self.highrise.chat(f"<#C0C0C0>❌ تم إلغاء تشغيل {radio_playlists.genre_display_name(genre)}")
                else:
                    await self.highrise.chat(f"<#C0C0C0>@{user.username} مفيش طلب منتظر")

            elif msg_lower == "!mex":
                all_songs = radio_playlists.get_all_songs()
                if not all_songs:
                    await self.highrise.chat("<#C0C0C0>📂 مفيش أغاني محفوظة لسه\nاطلب بـ !p وهتتحفظ تلقائياً")
                else:
                    _random.shuffle(all_songs)
                    mix = all_songs[:15]
                    await self.highrise.chat(f"<#FF4500>🎲 ميكس عشوائي - {len(mix)} أغنية...")
                    added = 0
                    for s in mix:
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.post(
                                    f"{self.RADIO_API_URL}/api/radio/add",
                                    json={"title": s["title"], "url": s["url"], "uploader": s.get("uploader",""), "duration": s.get("duration",0), "requestedBy": "!mex"},
                                    timeout=aiohttp.ClientTimeout(total=5)
                                ) as resp:
                                    if resp.status == 200:
                                        added += 1
                        except Exception:
                            pass
                    await self.highrise.chat(f"<#00FF00>✅ الميكس جاهز! {added} أغنية 🎶")
            # ==================== END RADIO COMMANDS ====================

            elif message.startswith("!info"):
                parts = message.split(" ")
                target_username = parts[1].replace("@", "").lower() if len(parts) > 1 else user.username.lower()
                room_users = (await self.highrise.get_room_users()).content
                for t_user, position in room_users:
                    if t_user.username.lower() == target_username:
                        is_admin = "Yes 👑" if t_user.username in self.ADMINS else "No 👤"
                        pos_str = f"({round(position.x, 1)}, {round(position.y, 1)}, {round(position.z, 1)})" if isinstance(position, Position) else "Unknown"
                        await self.highrise.chat(f"👤 Name: {t_user.username}\n🛡️ Admin: {is_admin}\n📍 Pos: {pos_str}")
                        break

            elif message.startswith("!love"):
                parts = message.split(" ")
                if len(parts) > 1:
                    percent = random.randint(0, 100)
                    await self.highrise.chat(f"💖 Love Compatibility: {percent}%")
                else: await self.highrise.chat("Usage: !love @username")

            elif message.startswith("!deathyear"):
                try:
                    parts = message.split(" ")
                    if len(parts) > 1:
                        target = parts[1]
                        future_year = random.randint(2050, 2120)
                        deaths = [
                            "of extreme old age while playing Highrise",
                            "winning a world championship in e-sports",
                            "after becoming the first human to live on Mars",
                            "peacefully in a pile of virtual gold",
                            "fighting a giant robot with a laser sword"
                        ]
                        reason = random.choice(deaths)
                        await self.highrise.chat(f"💀 <#FF0000>{target}, <#FFFF00>your estimated death year is <#00FF00>{future_year} <#FFFF00>{reason}! ⚰️")
                    else:
                        await self.highrise.chat("<#FF0000>Usage: !deathyear @username")
                except Exception as e:
                    print(f"Deathyear error: {e}")





            elif message.startswith("!hate"):
                try:
                    parts = message.split(" ")
                    if len(parts) > 1:
                        target = parts[1]
                        percent = random.randint(0, 100)
                        anger_icons = "💢" * (percent // 20) if percent > 0 else "😇"
                        await self.highrise.chat(f"😠 Hate Level between {user.username} and {target} is: {percent}% {anger_icons}")
                    else:
                        await self.highrise.chat("Usage: !hate @username")
                except Exception as e:
                    print(f"Error in hate: {e}")

            elif msg_lower == "!rizz":
                await self.highrise.chat(f"😏 <#FF69B4>{random.choice(self.rizz_lines)}")

            elif msg_lower == "!flirt":
                flirt_lines = [
                    "Are you a magician? Because whenever I look at you, everyone else disappears! ✨",
                    "Do you have a map? I keep getting lost in your eyes. 🗺️💕",
                    "Is your name Google? Because you have everything I've been searching for. 🔍❤️",
                    "If you were a vegetable, you'd be a cute-cumber! 🥒😊",
                    "Do you believe in love at first sight, or should I walk by again? 👀💖",
                    "Are you made of copper and tellurium? Because you're Cu-Te! ⚗️😍",
                    "I must be a snowflake, because I've fallen for you. ❄️💕",
                    "Is it hot in here, or is it just you? 🔥😘",
                    "You must be tired, because you've been running through my mind all day. 💭💓",
                    "If beauty were time, you'd be an eternity. ⏰✨"
                ]
                await self.highrise.chat(f"💕 <#FF1493>{random.choice(flirt_lines)}")

            elif msg_lower == "!joke":
                hindi_jokes = [
                    "Pappa: Beta, tere result ka kya hua?\nBeta: Headmaster ka beta fail ho gaya.\nPappa: Aur tu?\nBeta: Doctor ka beta bhi fail ho gaya.\nPappa: Main tere baare mein puch raha hoon!\nBeta: Toh aap kaun se bade collector hain, aapka beta bhi fail ho gaya!",
                    "Sharabi: Bottle mein se bhoot nikla aur bola, 'Kya hukum hai mere aaka?'\nSharabi: Ek chamcha dahi la de, mujhe kachumar banana hai!",
                    "Teacher: Pappu, batao 'A' ke baad kya aata hai?\nPappu: Ma'am, 'A' ke baad 'B' aata hai.\nTeacher: Aur 'B' ke baad?\nPappu: Ma'am, 'B' ke baad 'C' aur 'D' bhi aate hain, magar humein toh sirf 'B' se matlab hai!",
                    "Santa: Oye, tu kyun ro raha hai?\nBanta: Yaar, meri 1 kilo ki baraf kho gayi hai.\nSanta: Oye, fikr mat kar, baraf hi toh hai, agli baar 2 kilo le aana!",
                    "Master: 'Mazar' kise kehte hai?\nPappu: Jab koi mar jata hai, toh usey mazar kehte hai.\nMaster: Aur 'Guzar' kise kehte hai?\nPappu: Jab koi marne wala ho, toh usey guzar kehte hai.",
                    "Golu: Yaar, meri biwi ne mujhe ghar se nikal diya.\nBholu: Kyun?\nGolu: Usne pucha, 'Main kaisi lag rahi hoon?'\nMaine keh diya, 'Sasti, sundar aur tikau!'",
                    "Pati: Aaj khane mein kya banaya hai?\nBiwi: Gussa!\nPati: Toh khud hi kha lo, mera toh pet bhara hai!"
                ]
                await self.highrise.chat(f"😂 <#FFFF00>{random.choice(hindi_jokes)}")

            elif msg_lower == "!shayari":
                hindi_shayari = [
                    "Zindagi jine ka sahara chahiye,\nDil ko ek tera nazara chahiye,\nKhushiyan mile na mile duniya mein,\nGar mile tum toh ek jahan chahiye.",
                    "Dil ki dhadkan aur meri sadaa ho tum,\nMeri pehli aur aakhri wafa ho tum,\nChaha hai tumhe chahat se bhi zyada,\nMeri har khushi ki wajah ho tum.",
                    "Mohabbat ki shama jalakar toh dekho,\nZara humse nazrein milakar toh dekho,\nTumhe humse ishq na ho jaye toh kehna,\nBas ek baar hamari mehfil mein aakar toh dekho.",
                    "Haqiqat kaho toh unhe khwab lagta hai,\nShikwa karo toh unhe mazak lagta hai,\nKitni shiddat se hum unhe yaad karte hain,\nAur ek woh hain jinhe yeh sab ittefaq lagta hai.",
                    "Har phool ki ajab kahani hai,\nChup rehna bhi pyar ki nishani hai,\nKahin koi zakhm nahi phir bhi kyu dard ka ehsas hai,\nLagta hai dil ka ek tukda aaj bhi unke paas hai.",
                    "Aankhon mein raha dil mein utar kar nahi dekha,\nKashti ke musafir ne samandar nahi dekha,\nBewaqt agar jaunga toh sab chowk padenge,\nEk umr hui main ne apna ghar nahi dekha."
                ]
                await self.highrise.chat(f"💖 <#FF69B4>{random.choice(hindi_shayari)}")






            elif message.startswith("!roast"):
                try:
                    target = message.split(" ")[1]
                    await self.highrise.chat(f"🔥 <#FF8000>{target}, <#FF0000>{random.choice(self.roast_lines)}")
                except: await self.highrise.chat("Usage: !roast @username")

            # Teleport user to saved location

            elif message.startswith("!spam"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    if len(parts) >= 2:
                        try:
                            # Try parsing the last argument as the count
                            count = int(parts[-1])
                            # The message is everything in between command and count
                            text = " ".join(parts[1:-1])
                            
                            # Sanity check: cap at 100
                            count = min(count, 100)
                            
                            if not text:
                                await self.highrise.chat("Usage: !spam [message] [count]")
                                return

                            for _ in range(count):
                                await self.highrise.chat(text)
                                await asyncio.sleep(0.5)
                        except ValueError:
                            # Fallback: maybe they used the old format !spam [count] [message]
                            # Or just provided invalid number
                             await self.highrise.chat("Usage: !spam [message] [count]")
                    else:
                         await self.highrise.chat("Usage: !spam [message] [count]")









            elif message.startswith("!addadmin") or message.startswith("!admin "):
                if user.username in self.ADMINS:
                    try:
                        new_admin = message.split()[1].replace("@", "")
                        if new_admin not in self.settings.get("admins", []):
                            current_admins = self.settings.get("admins", [])
                            current_admins.append(new_admin)
                            self.settings["admins"] = current_admins
                            self.save_settings()
                            self.ADMINS = list(set(self.OWNERS + current_admins))
                            await self.highrise.chat(f"Added {new_admin} as admin!")
                        else:
                            await self.highrise.chat(f"@{new_admin} is already an admin!")
                    except:
                        await self.highrise.chat("Usage: !admin @username")

            elif message.startswith("!remadmin "):
                if user.username in self.ADMINS:
                    try:
                        target = message.split()[1].replace("@", "")
                        
                        # Prevent removing owners
                        if target in self.OWNERS:
                            await self.highrise.chat(f"<#FF0000>❌ Cannot remove @{target} - they are an Owner!")
                            return
                            
                        saved_admins = self.settings.get("admins", [])
                        if target in saved_admins:
                            saved_admins.remove(target)
                            self.settings["admins"] = saved_admins
                            self.save_settings()
                            
                            # Update runtime list
                            self.ADMINS = list(set(self.OWNERS + saved_admins))
                            
                            await self.highrise.chat(f"🛡️ Removed {target} from Admin status! ✅")
                        else:
                            await self.highrise.chat(f"⚠️ @{target} is not in the Admin list.")
                    except IndexError:
                        await self.highrise.chat("Usage: !remadmin @username")
                    except Exception as e:
                        print(f"Error removing admin: {e}")
                        await self.highrise.chat(f"<#FF0000>❌ Error: {e}")

            elif message.startswith("!owner "):
                if user.username in self.OWNERS:
                    try:
                        new_owner = message.split()[1].replace("@", "")
                        
                        # Add to saved owners list
                        saved_owners = self.settings.get("owners", [])
                        if new_owner not in saved_owners and new_owner not in self.hardcoded_owners:
                            saved_owners.append(new_owner)
                            self.settings["owners"] = saved_owners
                            self.save_settings()
                            
                            # Update runtime lists
                            self.OWNERS = list(set(self.hardcoded_owners + saved_owners))
                            self.ADMINS = list(set(self.OWNERS + self.settings.get("admins", [])))
                            
                            await self.highrise.chat(f"👑 Added @{new_owner} as an Owner! 🛡️\nThey now have access to ALL commands.")
                        else:
                            await self.highrise.chat(f"⚠️ @{new_owner} is already an Owner.")
                    except Exception as e:
                        print(f"Error adding owner: {e}")
                        await self.highrise.chat("Usage: !owner @username")
                else:
                    await self.highrise.chat("<#FF0000>Owner only!")

            elif msg_lower == "!ownerlist":
                if user.username in self.OWNERS:
                    # Build owner list message
                    owner_list = "\n".join([f"👑 @{o}" for o in self.OWNERS])
                    dm_msg = f"<#FFD700>📜 OWNER LIST 📜\n<#FFFFFF>{owner_list}\n\n<#888888>Total: {len(self.OWNERS)} owners"
                    
                    # Send via DM (whisper)
                    await self.highrise.send_whisper(user.id, dm_msg)
                    await self.highrise.chat(f"<#00FF00>📨 Owner list sent to your DM, @{user.username}!")
                else:
                    await self.highrise.chat("<#FF0000>Owner only!")


            # =========================================================
            # REACTION COMMANDS
            # =========================================================
            
            # --- Reaction All ---
            elif msg_lower in ["!heartall", "!winkall", "!thumball", "!thumbsall", "!waveall", "!clapall"]:
                if user.username in self.ADMINS:
                    reaction_map = {
                        "!heartall": ("heart", "❤️"),
                        "!winkall": ("wink", "😉"),
                        "!thumball": ("thumbs", "👍"),
                        "!thumbsall": ("thumbs", "👍"),
                        "!waveall": ("wave", "👋"),
                        "!clapall": ("clap", "👏")
                    }
                    react_type, emoji = reaction_map.get(msg_lower)
                    await self.highrise.chat(f"{emoji} Sending reactions to everyone! {emoji}")
                    
                    try:
                        room_users = (await self.highrise.get_room_users()).content
                        for target_user, _ in room_users:
                            try:
                                await self.highrise.react(react_type, target_user.id)
                                await asyncio.sleep(0.25)
                            except: pass
                    except Exception as e:
                        print(f"Error in reaction all: {e}")
                else:
                    await self.highrise.chat("❌ Moderator Only")
                return

            # --- Specific Reaction Spam ---
            elif msg_lower.startswith("h ") and (user.username in self.OWNERS or user.username in self.admins):
                # Shortcut: h username  →  send 25 hearts
                target_name = message.split(None, 1)[1].strip().replace("@", "")
                try:
                    room_users = (await self.highrise.get_room_users()).content
                    target_user = next((u for u, _ in room_users if u.username.lower() == target_name.lower()), None)
                    if target_user:
                        await self.highrise.chat(f"❤️ Sending 25 hearts to {target_user.username}...")
                        for _ in range(25):
                            try:
                                await self.highrise.react("heart", target_user.id)
                                await asyncio.sleep(0.4)
                            except: break
                        await self.highrise.chat(f"❤️ Done!")
                    else:
                        await self.highrise.chat(f"User @{target_name} not found.")
                except Exception as e:
                    print(f"Error in h-hearts: {e}")
                return

            elif msg_lower.startswith(("!heart ", "!wink ", "!thumb ", "!wave ", "!clap ")):
                # Usage: !heart @user [count]
                parts = message.split()
                if len(parts) >= 2:
                    cmd = parts[0].lower().replace("!", "")
                    target_name = parts[1].replace("@", "")
                    
                    reaction = cmd
                    if reaction == "thumb": reaction = "thumbs"
                    
                    count = 1
                    if len(parts) > 2:
                        try: count = int(parts[2])
                        except: pass
                    
                    count = min(count, 100) # Maximum 100 reactions allowed
                    
                    try:
                        room_users = (await self.highrise.get_room_users()).content
                        target_user = None
                        for u, _ in room_users:
                            if u.username.lower() == target_name.lower():
                                target_user = u
                                break
                        
                        if target_user:
                            await self.highrise.chat(f"Sending {count} {reaction}s to {target_user.username}... 🚀")
                            for _ in range(count):
                                try:
                                    await self.highrise.react(reaction, target_user.id)
                                    await asyncio.sleep(0.4) 
                                except: break # Stop if we hit an error (like user left)
                            await self.highrise.chat(f"Finished! ✅")
                        else:
                            await self.highrise.chat(f"User @{target_name} not found.")
                    except Exception as e:
                        print(f"Error in reaction spam: {e}")
                return
            
            elif message.startswith("!remowner "):
                if user.username in self.OWNERS:
                    try:
                        target = message.split()[1].replace("@", "")
                        
                        # Prevent removing hardcoded owner
                        if target in self.hardcoded_owners:
                            await self.highrise.chat(f"<#FF0000>❌ Cannot remove @{target} - they are a hardcoded owner!")
                            return
                        
                        removed_roles = []
                        
                        # 1. Remove from owners
                        saved_owners = self.settings.get("owners", [])
                        if target in saved_owners:
                            saved_owners.remove(target)
                            self.settings["owners"] = saved_owners
                            self.OWNERS = list(set(self.hardcoded_owners + saved_owners))
                            removed_roles.append("Owner")
                        
                        # 2. Remove from admins
                        saved_admins = self.settings.get("admins", [])
                        if target in saved_admins:
                            saved_admins.remove(target)
                            self.settings["admins"] = saved_admins
                            removed_roles.append("Admin")
                        
                        # 3. Update ADMINS list (owners + admins)
                        self.ADMINS = list(set(self.OWNERS + self.settings.get("admins", [])))
                        
                        # 4. Remove from blocked_users if present
                        if target in self.blocked_users:
                            self.blocked_users.remove(target)
                            self.settings["blocked_users"] = self.blocked_users
                            removed_roles.append("Blocked User")
                        
                        self.save_settings()
                        
                        if removed_roles:
                            roles_str = ", ".join(removed_roles)
                            await self.highrise.chat(f"<#00FF00>✅ Removed @{target}'s access!\n<#FFFF00>Removed roles: {roles_str}")
                        else:
                            await self.highrise.chat(f"<#FFFF00>⚠️ @{target} was not found in any role list.")
                    except IndexError:
                        await self.highrise.chat("Usage: !remowner @username")
                    except Exception as e:
                        print(f"Error removing owner: {e}")
                        await self.highrise.chat(f"<#FF0000>❌ Error: {e}")
                else:
                    await self.highrise.chat("<#FF0000>Owner only!")



            elif msg_lower == "!clear data":
                if user.username in self.OWNERS:
                    # 1. Clear VIPs
                    self.VIPS = []
                    self.settings["vips"] = []
                    
                    # 2. Clear Admins and Owners (Reset to Hardcoded Owners only)
                    self.settings["admins"] = []
                    self.settings["owners"] = []
                    self.OWNERS = list(self.hardcoded_owners)
                    self.ADMINS = list(self.hardcoded_owners)
                    
                    # 3. Clear Teleports
                    self.settings["user_teleports"] = {}
                    
                    # 4. Clear Subscribers
                    self.subscribers = []
                    self.settings["subscribers"] = []

                    # 5. Clear Statistics & Leaderboards
                    self.command_usage = {}
                    self.settings["command_usage"] = {}
                    
                    self.chat_stats = {}
                    self.settings["chat_stats"] = {}
                    
                    self.user_visits = {}
                    self.settings["user_visits"] = {}
                    
                    self.total_tips = 0
                    self.settings["total_tips"] = 0
                    self.tip_history = []
                    self.settings["tip_history"] = []

                    # 6. Clear Time Tracking
                    self.user_times = {}
                    self.settings["user_times"] = {}

                    # 7. Clear Moderation
                    self.banned_users = {}
                    self.settings["banned_users"] = {}
                    self.warnings = {}
                    self.settings["warnings"] = {}
                    self.blocked_users = []
                    self.settings["blocked_users"] = []


                    # 9. Reset Broadcast & Emote Settings
                    self.broadcast_message = ""
                    self.settings["broadcast_msg"] = self.broadcast_message
                    self.playing_all_emotes = False # Stop loop
                    
                    # Save changes
                    self.save_settings()
                    
                    await self.highrise.chat("<#FF0000>⚠️ SYSTEM RESET COMPLETE! ⚠️\n<#FFFF00>All data (VIPs, Admins, Stats, Time, Logs, Settings) has been CLEARED.")
                else:
                    await self.highrise.chat("<#FF0000>Only Owners can clear data!")

            elif message.startswith("!addvip"):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        new_vip = parts[1].replace("@", "")
                        duration_arg = parts[2].lower() if len(parts) > 2 else "permanent"
                        
                        # Calculate expiration
                        expiration = "permanent"
                        if duration_arg != "permanent":
                            try:
                                days = int(duration_arg)
                                expiration = int(time.time() + (days * 86400)) # timestamp
                            except ValueError:
                                await self.highrise.chat("❌ Invalid duration! Use days (e.g. 30) or 'permanent'.")
                                return

                        # Migrate self.VIPS to dict if it's a list
                        if isinstance(self.VIPS, list):
                            self.VIPS = {v: "permanent" for v in self.VIPS}
                        
                        self.VIPS[new_vip] = expiration
                        self.settings["vips"] = self.VIPS
                        
                        # Add to history
                        if "vip_history" not in self.settings: self.settings["vip_history"] = []
                        if new_vip not in self.settings["vip_history"]:
                            self.settings["vip_history"].append(new_vip)
                        
                        self.save_settings()
                        
                        if expiration == "permanent":
                            await self.highrise.chat(f"💎 Added @{new_vip} as a Permanent VIP! 👑")
                        else:
                            await self.highrise.chat(f"💎 Added @{new_vip} as VIP for {duration_arg} days! ⏳")
                    except IndexError:
                        await self.highrise.chat("Usage: !addvip @username [days/permanent]")

            elif message.startswith("!removevip"):
                if user.username in self.ADMINS:
                    try:
                        target_vip = message.split()[1].replace("@", "")
                        
                        # Handle migration if list
                        if isinstance(self.VIPS, list):
                            self.VIPS = {v: "permanent" for v in self.VIPS}

                        if target_vip in self.VIPS:
                            del self.VIPS[target_vip]
                            self.settings["vips"] = self.VIPS
                            self.save_settings()
                            await self.highrise.chat(f"🗑️ Removed {target_vip} from VIPs!")
                        else:
                            await self.highrise.chat(f"❌ {target_vip} is not a VIP.")
                    except IndexError:
                        await self.highrise.chat("Usage: !removevip @username")

            elif message.startswith("!vipstatus"):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        # Handle list vs dict
                        if isinstance(self.VIPS, list):
                            self.VIPS = {v: "permanent" for v in self.VIPS}

                        if target_name in self.VIPS:
                            raw_exp = self.VIPS[target_name]
                            if raw_exp == "permanent":
                                expiry = "Permanent"
                            else:
                                # Convert timestamp to readable date
                                days_left = int((raw_exp - time.time()) / 86400)
                                if days_left < 0: days_left = 0
                                expiry = f"{days_left} days left"
                            
                            status = "Active VIP 👑"
                        else:
                            status = "Not a VIP ❌"
                            expiry = "N/A"
                        
                        await self.highrise.chat(f"📊 VIP Status for @{target_name}:\nStatus: {status}\nExpires: {expiry}")
                    except IndexError:
                        await self.highrise.chat("Usage: !vipstatus @username")

            elif msg_lower == "!cleanupvip" or msg_lower == "!cleanvip":
                if user.username in self.ADMINS:
                    # Handle migration if list
                    if isinstance(self.VIPS, list):
                        self.VIPS = {v: "permanent" for v in self.VIPS}
                    
                    removed = []
                    current_time = time.time()
                    for name, exp in list(self.VIPS.items()):
                        if exp != "permanent" and isinstance(exp, (int, float)):
                            if current_time > exp:
                                del self.VIPS[name]
                                removed.append(name)
                    
                    if removed:
                        self.settings["vips"] = self.VIPS
                        self.save_settings()
                        await self.highrise.chat(f"🧹 Cleanup Complete! Removed {len(removed)} expired VIPs: {', '.join(removed)}")
                    else:
                        await self.highrise.chat("🧹 System Check: No expired VIPs found.")

            elif message.startswith("!goto "):
                if user.username in self.ADMINS:
                    try:
                        target = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        target_pos = None
                        for u, p in room_users:
                            if u.username.lower() == target.lower():
                                target_pos = p
                                break
                        
                        if target_pos and isinstance(target_pos, Position):
                            await self.highrise.teleport(user.id, target_pos)
                            await self.highrise.chat(f"🚀 Teleported to @{target}!")
                        elif target_pos and isinstance(target_pos, AnchorPosition):
                             await self.highrise.chat(f"❌ @{target} is on an anchor, cannot teleport directly.")
                        else:
                            await self.highrise.chat(f"❌ Could not find @{target} in the room.")
                    except IndexError:
                        await self.highrise.chat("Usage: !goto @username")
                else:
                     await self.highrise.chat("<#FF0000>Admins only!")

            elif message.startswith("!switch "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        
                        user_pos = None
                        target_pos = None
                        target_user_id = None
                        
                        for u, p in room_users:
                            if u.id == user.id:
                                user_pos = p
                            if u.username.lower() == target_name.lower():
                                target_pos = p
                                target_user_id = u.id
                                
                        if not user_pos:
                             await self.highrise.chat("❌ Could not find your position.")
                             return
                        if not target_pos:
                             await self.highrise.chat(f"❌ Could not find @{target_name}.")
                             return
                             
                        if isinstance(user_pos, AnchorPosition) or isinstance(target_pos, AnchorPosition):
                            await self.highrise.chat("❌ Cannot switch with users on anchors/furniture.")
                            return
                            
                        # Perform Switch
                        await self.highrise.teleport(user.id, target_pos)
                        await self.highrise.teleport(target_user_id, user_pos)
                        await self.highrise.chat(f"🔄 Switched places with @{target_name}!")
                        
                    except IndexError:
                        await self.highrise.chat("Usage: !switch @username")
                else:
                     await self.highrise.chat("<#FF0000>Admins only!")



            elif msg_lower == "!viplist":
                if not self.VIPS:
                    await self.highrise.chat("📜 The VIP list is currently empty.")
                else:
                    v_lines = []
                    current_time = time.time()
                    
                    if isinstance(self.VIPS, list):
                        for name in self.VIPS:
                            v_lines.append(f"• @{name} (Permanent)")
                    else:
                        for name, exp in self.VIPS.items():
                            if exp == "permanent":
                                v_lines.append(f"• @{name} (Permanent)")
                            else:
                                try:
                                    days_left = int((exp - current_time) / 86400)
                                    if days_left < 0: days_left = 0
                                    v_lines.append(f"• @{name} ({days_left} days left)")
                                except:
                                    v_lines.append(f"• @{name} (Unknown)")
                    
                    header = "👑 **CURRENT ROOM VIPS** 👑\n"
                    await self.safe_chat(header + "\n".join(v_lines))

            elif msg_lower == "!wallet":
                try:
                    wallet = (await self.highrise.get_wallet()).content
                    gold = 0
                    for item in wallet:
                        if item.type == 'gold':
                            gold = item.amount
                            break
                    await self.highrise.chat(trans["wallet"].format(g=gold))
                except Exception as e:
                    print(f"Wallet error: {e}")
                    await self.highrise.chat("<#FF0000>Could not fetch wallet balance.")

            elif message.startswith("!freeze "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        
                        target_user = None
                        target_pos = None
                        for u, p in room_users:
                            if u.username.lower() == target_name.lower():
                                target_user = u
                                target_pos = p
                                break
                        
                        if target_user:
                             self.frozen_users[target_user.id] = target_pos
                             try:
                                 await self.highrise.moderate_room(target_user.id, "mute", 3600) # Mute for 1 hour
                             except: pass
                             
                             await self.highrise.chat(f"🥶 @{target_name} has been FROZEN! They cannot move or speak. 🤐")
                             await self.report_moderation_action("Freeze", target_name, user.username)
                        else:
                             await self.highrise.chat(f"❌ Could not find @{target_name}.")
                    except IndexError:
                        await self.highrise.chat("Usage: !freeze @username")
                else:
                     await self.highrise.chat("<#FF0000>Admins only!")

            elif message.startswith("!unfreeze "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        
                        target_user = None
                        for u, p in room_users:
                            if u.username.lower() == target_name.lower():
                                target_user = u
                                break
                        
                        if target_user:
                             if target_user.id in self.frozen_users:
                                 del self.frozen_users[target_user.id]
                             
                             try:
                                 await self.highrise.moderate_room(target_user.id, "mute", 1) # Unmute
                             except: pass
                             
                             await self.highrise.chat(f"🔥 @{target_name} has been UNFROZEN! You are free! 🕊️")
                             await self.report_moderation_action("Unfreeze", target_name, user.username)
                        else:
                             # Try to find by ID in frozen list? Hard without name map, just say not found
                             await self.highrise.chat(f"❌ User @{target_name} not found in room (or list).")
                    except IndexError:
                        await self.highrise.chat("Usage: !unfreeze @username")
                else:
                     await self.highrise.chat("<#FF0000>Admins only!")


            elif message.startswith("!getfit "):
                # Public command
                try:
                    target_name = message[8:].strip().replace("@", "")
                    if not target_name:
                        await self.highrise.chat("Usage: !getfit @username")
                        return
                    
                    target_id = None
                    
                    # 1. Search in Room (with Fuzzy/Partial Match for Bots)
                    room_users = (await self.highrise.get_room_users()).content
                    # Exact match first
                    for u, _ in room_users:
                        if u.username.lower() == target_name.lower():
                            target_id = u.id
                            break
                    
                    # Partial match if not found (useful for bots with tags or prefix)
                    if not target_id:
                        for u, _ in room_users:
                            if target_name.lower() in u.username.lower():
                                target_id = u.id
                                target_name = u.username # Update name to found user
                                break
                    
                    # 2. Search via Web API (if not in room)
                    if not target_id:
                        if self.webapi:
                            try:
                                await self.highrise.chat(f"🔍 Searching @{target_name} globally... 🌎")
                                user_resp = await self.webapi.get_users(username=target_name)
                                if user_resp and hasattr(user_resp, 'users') and user_resp.users:
                                    target_id = user_resp.users[0].user_id
                                    print(f"[DEBUG] Found target_id via global search: {target_id}")
                                else:
                                    # Fallback: Try a direct ID fetch anyway if search fails
                                    # This works for Official Bots and hidden profiles
                                    await self.highrise.chat(f"⏳ Global search dry... Trying direct Cloud Link for @{target_name}... ☁️")
                                    target_id = target_name # Try treating it as ID directly
                                    print(f"[DEBUG] Search empty, attempting direct fetch with: {target_id}")
                            except Exception as e:
                                await self.highrise.chat(f"❌ WebAPI Search Error: {e}")
                                return
                        else:
                            await self.highrise.chat(f"❌ User @{target_name} not found.")
                            return

                    target_outfit = None
                    
                    # 3. Fetch Outfit - Bot-Centric WebAPI priority
                    if self.webapi:
                        try:
                            # Use WebAPI search even if in room to get the CLEANEST bot/user outfit data
                            await self.highrise.chat(f"🧩 Fetching Cloud Outfit for @{target_name}... ☁️")
                            target_outfit = await self.webapi.get_outfit(target_name)
                            if target_outfit:
                                print(f"[DEBUG] Successfully got cloud outfit for {target_name}")
                        except Exception as e:
                            print(f"[DEBUG] WebAPI Outfit fetch failed, trying Gateway: {e}")

                    # Fallback to Gateway if WebAPI failed or not available
                    if not target_outfit and target_id:
                        try:
                            print(f"[DEBUG] Trying Gateway outfit fetch for {target_id}")
                            outfit_result = await self.highrise.get_user_outfit(target_id)
                            # SDK might return a Response object with .outfit or .content.outfit
                            if hasattr(outfit_result, 'outfit') and isinstance(outfit_result.outfit, list):
                                target_outfit = outfit_result.outfit
                            elif hasattr(outfit_result, 'content') and hasattr(outfit_result.content, 'outfit'):
                                target_outfit = outfit_result.content.outfit
                            elif isinstance(outfit_result, list):
                                target_outfit = outfit_result
                        except Exception as e:
                            print(f"[DEBUG] Gateway outfit fetch failed: {e}")
                            
                    # Final Validation: Ensure target_outfit is an iterable list for set_outfit
                    if target_outfit and not isinstance(target_outfit, list):
                        print(f"[DEBUG] target_outfit was not a list ({type(target_outfit)}), discarding.")
                        target_outfit = None

                    # Apply the outfit if found via any method
                    if target_outfit and len(target_outfit) > 0:
                        try:
                            # Apply the outfit to the bot
                            await self.highrise.set_outfit(target_outfit)
                            await self.highrise.chat(f"👔 Copied outfit from @{target_name}! 🎨")
                        except Exception as e:
                            # If it fails with a specific item, the bot usually stays previous.
                            await self.highrise.chat(f"❌ Failed to set outfit: {str(e)[:50]}")
                    elif target_outfit is not None and len(target_outfit) == 0:
                         await self.highrise.chat(f"👤 @{target_name} is not wearing any compatible items.")
                    else:
                        await self.highrise.chat(f"❌ Could not retrieve outfit for @{target_name}. Profile might be restricted or offline.")
                except IndexError:
                    await self.highrise.chat("Usage: !getfit @username")
                except Exception as e:
                    await self.highrise.chat(f"❌ Error processing command: {e}")


            elif message.startswith("!cashout") or message.startswith("!withdraw"):
                if user.username in self.OWNERS:
                    try:
                        wallet = (await self.highrise.get_wallet()).content
                        bot_gold = 0
                        for item in wallet:
                            if item.type == 'gold':
                                bot_gold = item.amount
                                break
                        
                        if bot_gold <= 0:
                            await self.highrise.chat("❌ The bot wallet is empty.")
                            return
                        
                        parts = message.split()
                        amount = bot_gold
                        if len(parts) > 1:
                            if parts[1].lower() != "all":
                                amount = min(int(parts[1]), bot_gold)
                        
                        if amount <= 0:
                            await self.highrise.chat("❌ Invalid amount.")
                            return

                        await self.highrise.chat(f"💰 Processing cashout of {amount} Gold to @{user.username}...")
                        
                        BARS = [
                            (10000, "gold_bar_10000"), (5000, "gold_bar_5000"),
                            (1000, "gold_bar_1000"), (500, "gold_bar_500"),
                            (100, "gold_bar_100"), (50, "gold_bar_50"),
                            (10, "gold_bar_10"), (5, "gold_bar_5"),
                            (1, "gold_bar_1")
                        ]
                        
                        rem_amount = amount
                        for bar_val, bar_id in BARS:
                            while rem_amount >= bar_val:
                                try:
                                    await self.highrise.tip_user(user.id, bar_id)
                                    rem_amount -= bar_val
                                    await asyncio.sleep(0.4)
                                except Exception as e:
                                    print(f"Tip error ({bar_id}): {e}")
                                    break
                        
                        await self.highrise.chat(f"✅ Cashout complete! Sent {amount - rem_amount} Gold to you! 💸")
                    except Exception as e:
                        print(f"Cashout error: {e}")
                        await self.highrise.chat("❌ Error during cashout.")
                else:
                    await self.highrise.chat("<#FF0000>Only Owners can cashout!")

            elif message.startswith("!tipall "):
                if user.username in self.ADMINS:
                    try:
                        # Check Balance properly
                        wallet = (await self.highrise.get_wallet()).content
                        bot_gold = 0
                        for item in wallet:
                            if item.type == 'gold':
                                bot_gold = item.amount
                                break
                        
                        parts = message.split()
                        amount_val = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                        tip_ids = {"1": "gold_bar_1", "5": "gold_bar_5", "10": "gold_bar_10", "50": "gold_bar_50", "100": "gold_bar_100", "500": "gold_bar_500", "1000": "gold_bar_1000", "5000": "gold_bar_5000", "10000": "gold_bar_10000"}
                        
                        if str(amount_val) not in tip_ids:
                            await self.highrise.chat(f"<#FF0000>❌ Invalid amount! Use: {', '.join(tip_ids.keys())}")
                            return
                        
                        room_users = (await self.highrise.get_room_users()).content
                        to_tip = [u for u, _ in room_users if u.id != self.bot_id]
                        total_needed = len(to_tip) * amount_val
                        
                        if bot_gold < total_needed:
                            await self.highrise.chat(f"❌ Bot has {bot_gold}G, but you need {total_needed}G to tip everyone {amount_val}G.")
                            return

                        await self.highrise.chat(f"💰 Tipping {amount_val} Gold to {len(to_tip)} users... 🚀")
                        
                        count = 0
                        for r_user in to_tip:
                            try:
                                await self.highrise.tip_user(r_user.id, tip_ids[str(amount_val)])
                                count += 1
                                await asyncio.sleep(0.5) # Prevent spam kick
                            except: continue
                        
                        await self.highrise.chat(f"✅ <#00FF00>Successfully tipped {amount_val} Gold to {count} users! 🎉")
                    except: await self.highrise.chat("Usage: !tipall [amount]")

            elif message.startswith("!tip "):
                if user.username in self.ADMINS:
                    try:
                        # Check Balance first
                        wallet = (await self.highrise.get_wallet()).content
                        bot_gold = 0
                        for item in wallet:
                            if item.type == 'gold':
                                bot_gold = item.amount
                                break
                        if bot_gold <= 0:
                            await self.highrise.chat("bot have insufficient balance")
                            return

                        parts = message.split()
                        if len(parts) < 3:
                            await self.highrise.chat("Usage: !tip @username [amount] OR !tip all [amount]")
                            return
                        
                        target_name = parts[1].replace("@", "").lower()
                        amount = parts[2]
                        tip_ids = {"1": "gold_bar_1", "5": "gold_bar_5", "10": "gold_bar_10", "50": "gold_bar_50", "100": "gold_bar_100", "500": "gold_bar_500", "1000": "gold_bar_1000", "5000": "gold_bar_5000", "10000": "gold_bar_10000"}
                        
                        if amount not in tip_ids:
                            await self.highrise.chat(f"<#FF0000>❌ Invalid amount! Use: {', '.join(tip_ids.keys())}")
                            return

                        if target_name == "all":
                            room_users = (await self.highrise.get_room_users()).content
                            count = 0
                            for r_user, _ in room_users:
                                if r_user.id != self.bot_id:
                                    try:
                                        await self.highrise.tip_user(r_user.id, tip_ids[amount])
                                        count += 1
                                        await asyncio.sleep(0.5) 
                                    except: continue
                            await self.highrise.chat(f"💰 <#00FF00>Tipped {amount} Gold to {count} users! 🎉")
                            return

                        room_users = (await self.highrise.get_room_users()).content
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                await self.highrise.tip_user(r_user.id, tip_ids[amount])
                                await self.highrise.chat(f"💰 <#00FF00>Tipped {amount} Gold to @{r_user.username}! ✨")
                                return
                        await self.highrise.chat("User not found.")
                    except: await self.highrise.chat("Usage: !tip @username [amount] OR !tip all [amount]")

            elif message.startswith("!tipme "):
                if user.username in self.ADMINS:
                    try:
                        # Check Balance first
                        wallet = (await self.highrise.get_wallet()).content
                        bot_gold = 0
                        for item in wallet:
                            if item.type == 'gold':
                                bot_gold = item.amount
                                break
                        if bot_gold <= 0:
                            await self.highrise.chat("bot have insufficient balance")
                            return

                        amount = int(message.split()[1])
                        if amount <= 0:
                            await self.highrise.chat("❌ Enter a valid amount.")
                            return
                            
                        # Denominations for tipping
                        BARS = [
                            (10000, "gold_bar_10000"), (5000, "gold_bar_5000"),
                            (100, "gold_bar_100"), (50, "gold_bar_50"),
                            (10, "gold_bar_10"), (5, "gold_bar_5"),
                            (1, "gold_bar_1")
                        ]
                        
                        rem = amount
                        tipped = 0
                        for val, b_id in BARS:
                            while rem >= val:
                                try:
                                    await self.highrise.tip_user(user.id, b_id)
                                    rem -= val
                                    tipped += val
                                    await asyncio.sleep(0.4)
                                except: break
                                
                        await self.highrise.chat(f"💰 <#00FF00>Success! Tipped {tipped} Gold to you, @{user.username}! ✨")
                    except: await self.highrise.chat("Usage: !tipme [amount]")






            elif msg_lower == "!restartbot":
                if user.username in self.ADMINS:
                    await self.highrise.chat("🔄 <#00FF00>Initiating system reboot... The bot will be offline for about 30 seconds. ⏳")
                    await asyncio.sleep(2)
                    # Exit the process. run.py will detect the stop and restart it after the cooldown.
                    sys.exit(0)

            elif message.startswith("!create tele "):
                if user.username in self.ADMINS:
                    try:
                        location_name = message.split(" ", 2)[2].lower()
                        room_users = (await self.highrise.get_room_users()).content
                        user_pos = None
                        for u, p in room_users:
                            if u.id == user.id:
                                user_pos = p
                                break
                        
                        if not user_pos:
                            await self.highrise.chat("❌ Could not find your position. Are you in the room?")
                            return

                        if isinstance(user_pos, AnchorPosition):
                             await self.highrise.chat("❌ You are on an anchor. Please stand on the floor.")
                             return

                        self.locations[location_name] = {
                            "x": user_pos.x,
                            "y": user_pos.y,
                            "z": user_pos.z,
                            "facing": getattr(user_pos, "facing", "FrontRight"),
                            "vip_only": False
                        }
                        with open(self.location_file, "w") as f:
                            json.dump(self.locations, f)
                            
                        await self.highrise.chat(f"✅ Created teleport '{location_name}' at ({user_pos.x:.1f}, {user_pos.y:.1f}, {user_pos.z:.1f})! 📍")
                    except Exception as e:
                        print(f"Create tele error: {e}")
                        await self.highrise.chat("Usage: !create tele [name]")
                else:
                    await self.highrise.chat("<#FF0000>Admins only!")

            elif msg_lower == "!telelist":
                if not self.locations:
                    await self.highrise.chat("Creating NO teleport locations yet.")
                else:
                    public_locs = [name for name, data in self.locations.items() if not data.get("vip_only") and not data.get("mod_only") and not data.get("owner_only")]
                    vip_locs = [name for name, data in self.locations.items() if data.get("vip_only")]
                    mod_locs = [name for name, data in self.locations.items() if data.get("mod_only")]
                    owner_locs = [name for name, data in self.locations.items() if data.get("owner_only")]
                    
                    msg = "📍 Teleport Locations:\n"
                    if public_locs:
                        msg += f"<#00FF00>🌍 Public: <#FFFFFF>{', '.join(public_locs)}\n"
                    if vip_locs:
                        msg += f"<#FFD700>👑 VIP: <#FFFFFF>{', '.join(vip_locs)}\n"
                    if mod_locs:
                        msg += f"<#00FFFF>🛡️ Mod: <#FFFFFF>{', '.join(mod_locs)}\n"
                    if owner_locs:
                        msg += f"<#FF00FF>🔑 Owner: <#FFFFFF>{', '.join(owner_locs)}"
                    
                    if not any([public_locs, vip_locs, mod_locs, owner_locs]):
                         msg = "No locations found."
                    
                    await self.safe_chat(msg)

            elif msg_lower == "!cleartele":
                if user.username in self.ADMINS:
                    self.locations = {}
                    with open(self.location_file, "w") as f:
                        json.dump(self.locations, f)
                    await self.highrise.chat("🗑️ All teleport locations cleared!")
                else:
                    await self.highrise.chat("<#FF0000>Admins only!")

            elif message.startswith("!remtele "):
                if user.username in self.ADMINS:
                    try:
                        location_name = message.split(None, 1)[1].lower()
                        
                        if location_name in self.locations:
                            loc_data = self.locations[location_name]
                            del self.locations[location_name]
                            
                            with open(self.location_file, "w") as f:
                                json.dump(self.locations, f)
                            
                            await self.highrise.chat(f"🗑️ Removed teleport '{location_name}' (Position: {loc_data['x']:.1f}, {loc_data['y']:.1f}, {loc_data['z']:.1f})")
                        else:
                            await self.highrise.chat(f"❌ Teleport '{location_name}' not found.")
                    except IndexError:
                        await self.highrise.chat("Usage: !remtele [name]")
                else:
                    await self.highrise.chat("<#FF0000>Admins only!")

            elif message.startswith("!createvip tele "):
                if user.username in self.ADMINS:
                    try:
                        location_name = message.split(" ", 2)[2].lower()
                        room_users = (await self.highrise.get_room_users()).content
                        user_pos = None
                        for u, p in room_users:
                            if u.id == user.id:
                                user_pos = p
                                break
                        
                        if not user_pos:
                            await self.highrise.chat("❌ Could not find your position.")
                            return
                        if isinstance(user_pos, AnchorPosition):
                             await self.highrise.chat("❌ You are on an anchor. Stand on the floor.")
                             return

                        self.locations[location_name] = {
                            "x": user_pos.x,
                            "y": user_pos.y,
                            "z": user_pos.z,
                            "facing": getattr(user_pos, "facing", "FrontRight"),
                            "vip_only": True
                        }
                        with open(self.location_file, "w") as f:
                            json.dump(self.locations, f)
                            
                        await self.highrise.chat(f"💎 Created VIP teleport '{location_name}'! 👑")
                    except Exception as e:
                         await self.highrise.chat("Usage: !createvip tele [name]")
                else:
                    await self.highrise.chat("<#FF0000>Admins only!")

            elif message.startswith("!createmod tele "):
                if user.username in self.ADMINS:
                    try:
                        location_name = message.split(" ", 2)[2].strip().lower()
                        room_users = (await self.highrise.get_room_users()).content
                        user_pos = None
                        for u, p in room_users:
                            if u.id == user.id:
                                user_pos = p
                                break
                        
                        if not user_pos:
                            await self.highrise.chat("❌ Could not find your position.")
                            return
                        if isinstance(user_pos, AnchorPosition):
                             await self.highrise.chat("❌ You are on an anchor. Stand on the floor.")
                             return

                        self.locations[location_name] = {
                            "x": user_pos.x,
                            "y": user_pos.y,
                            "z": user_pos.z,
                            "facing": getattr(user_pos, "facing", "FrontRight"),
                            "mod_only": True
                        }
                        with open(self.location_file, "w") as f:
                            json.dump(self.locations, f)
                            
                        await self.highrise.chat(f"🛡️ Created MOD teleport '{location_name}'! ✅")
                    except Exception as e:
                         await self.highrise.chat("Usage: !createmod tele [name]")
                else:
                    await self.highrise.chat("<#FF0000>Admins only!")

            elif message.startswith("!createowner tele "):
                if user.username in self.OWNERS:
                    try:
                        location_name = message.split(" ", 2)[2].strip().lower()
                        room_users = (await self.highrise.get_room_users()).content
                        user_pos = None
                        for u, p in room_users:
                            if u.id == user.id:
                                user_pos = p
                                break
                        
                        if not user_pos:
                            await self.highrise.chat("❌ Could not find your position.")
                            return
                        if isinstance(user_pos, AnchorPosition):
                             await self.highrise.chat("❌ You are on an anchor. Stand on the floor.")
                             return

                        self.locations[location_name] = {
                            "x": user_pos.x,
                            "y": user_pos.y,
                            "z": user_pos.z,
                            "facing": getattr(user_pos, "facing", "FrontRight"),
                            "owner_only": True
                        }
                        with open(self.location_file, "w") as f:
                            json.dump(self.locations, f)
                            
                        await self.highrise.chat(f"🔑 Created OWNER teleport '{location_name}'! 🚀")
                    except Exception as e:
                         await self.highrise.chat("Usage: !createowner tele [name]")
                else:
                    await self.highrise.chat("<#FF0000>Owners only!")








            elif message.lower().startswith("!summon"):
                # Usage: !summon @username OR !summon all
                if any(o.lower() == user.username.lower() for o in self.ADMINS):
                    try:
                        args = message.split()
                        if len(args) < 2:
                            await self.highrise.chat("💡 Usage: !summon @username OR !summon all")
                            return
                            
                        target_arg = args[1].lower()
                        
                        # 1. Get Caller Position
                        room_users_resp = await self.highrise.get_room_users()
                        room_users = room_users_resp.content
                        caller_pos = None
                        for u, p in room_users:
                            if u.id == user.id:
                                caller_pos = p
                                break
                        
                        if not caller_pos:
                            await self.highrise.chat("❌ I couldn't find your position. Try moving and use again.")
                            return

                        # Ensure we have a Position object for teleporting others to
                        if isinstance(caller_pos, AnchorPosition):
                            await self.highrise.chat("❌ You are on an anchor. Please stand on the floor to summon.")
                            return
                            
                        # Extract exact coordinates to ensure reliability
                        dest_pos = Position(caller_pos.x, caller_pos.y, caller_pos.z, caller_pos.facing)

                        # 2. Handle Summon
                        if target_arg in ["all", "@all"]:
                             await self.highrise.chat(f"🔮 Bringing EVERYONE to you! 🌀")
                             await summon_all(self, dest_pos, exclude_user_id=user.id)
                        else:
                             target_name = args[1].replace("@", "")
                             success = await summon_user(self, target_name, dest_pos)
                             if success:
                                 await self.highrise.chat(f"🔮 Brought @{target_name} to you! 🌀")
                             else:
                                 await self.highrise.chat(f"❌ Could not summon @{target_name}. Make sure they are in the room.")
                    except Exception as e:
                        print(f"Summon command error: {e}")
                        import traceback
                        traceback.print_exc()
                        await self.highrise.chat("❌ An error occurred with the summon command.")
                else:
                    await self.highrise.chat("<#FF0000>Admins only!")



            elif message.startswith("!tipall "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        if len(parts) < 2:
                             await self.highrise.chat("Usage: !tipall [amount]")
                             return
                        
                        amount = int(parts[1])
                        str_amount = str(amount)
                        tip_ids = {"1": "gold_bar_1", "5": "gold_bar_5", "10": "gold_bar_10", "50": "gold_bar_50", "100": "gold_bar_100", "500": "gold_bar_500", "1000": "gold_bar_1000", "5000": "gold_bar_5000", "10000": "gold_bar_10000"}
                        
                        if str_amount not in tip_ids:
                            await self.highrise.chat(f"<#FF0000>❌ Invalid amount! Use: {', '.join(tip_ids.keys())}")
                            return
                        
                        # Check Wallet Balance
                        try:
                            wallet = (await self.highrise.get_wallet()).content
                            gold_amount = 0
                            for currency in wallet:
                                if currency.type == 'gold':
                                    gold_amount = currency.amount
                                    break
                        except Exception as e:
                            print(f"Wallet check error: {e}")
                            gold_amount = 999999 # Fallback if wallet check fails to allow try

                        room_users = (await self.highrise.get_room_users()).content
                        targets = [u.id for u, _ in room_users if u.id != self.bot_id]
                        
                        if not targets:
                             await self.highrise.chat("❌ No one else is in the room!")
                             return

                        total_cost = amount * len(targets)
                        
                        if gold_amount < total_cost:
                             await self.highrise.chat(f"❌ Not enough funds! Needed: {total_cost} Gold, Have: {gold_amount} Gold.")
                             return
                        
                        count = 0
                        bar_id = tip_ids[str_amount]
                        for r_user_id in targets:
                            try:
                                await self.highrise.tip_user(r_user_id, bar_id)
                                count += 1
                                await asyncio.sleep(0.5) # Prevent spam kick
                            except: continue
                        
                        await self.highrise.chat(f"💰 <#00FF00>Tipped {amount} Gold to {count} users! Total: {amount*count} Gold. 🎉")
                    except Exception as e:
                        print(f"Tipall error: {e}")
                        await self.highrise.chat("Usage: !tipall [amount]")

            elif message.startswith("!tip "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        if len(parts) < 3:
                            await self.highrise.chat("Usage: !tip @username [amount]")
                            return

                        target_name = parts[1].replace("@", "").lower()
                        amount = int(parts[2])
                        str_amount = str(amount)
                        tip_ids = {"1": "gold_bar_1", "5": "gold_bar_5", "10": "gold_bar_10", "50": "gold_bar_50", "100": "gold_bar_100", "500": "gold_bar_500", "1000": "gold_bar_1000", "5000": "gold_bar_5000", "10000": "gold_bar_10000"}
                        
                        if str_amount not in tip_ids:
                            await self.highrise.chat(f"<#FF0000>❌ Invalid amount! Use: {', '.join(tip_ids.keys())}")
                            return

                        if target_name == "all":
                             # Redirect to tipall logic
                             await self.on_chat(user, f"!tipall {amount}")
                             return

                         # Check Wallet Balance
                        try:
                            wallet = (await self.highrise.get_wallet()).content
                            gold_amount = 0
                            for currency in wallet:
                                if currency.type == 'gold':
                                    gold_amount = currency.amount
                                    break
                            
                            if gold_amount < amount:
                                await self.highrise.chat(f"❌ Not enough funds! Needed: {amount}, Have: {gold_amount}")
                                return
                        except Exception as e:
                             print(f"Wallet check error: {e}")

                        room_users = (await self.highrise.get_room_users()).content
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                await self.highrise.tip_user(r_user.id, tip_ids[str_amount])
                                await self.highrise.chat(f"💰 <#00FF00>Tipped {amount} Gold to @{r_user.username}! ✨")
                                return
                        await self.highrise.chat("User not found.")
                    except: await self.highrise.chat("Usage: !tip @username [amount]")

            elif message.startswith("!ban "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        target_name = parts[1].replace("@", "").lower()
                        minutes = int(parts[2]) if len(parts) > 2 else 300 # Default 300 mins
                        duration = minutes * 60 
                        
                        room_users = (await self.highrise.get_room_users()).content
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                try:
                                    await self.highrise.moderate_room(r_user.id, "ban", duration)
                                    self.banned_users[r_user.username.lower()] = r_user.id
                                    self.settings["banned_users"] = self.banned_users
                                    self.save_settings()
                                    await self.highrise.chat(f"🚫 <#FF0000>@{r_user.username} has been banned for {minutes} minutes!")
                                    await self.report_moderation_action(f"Ban ({minutes}m)", r_user.username, user.username)
                                except Exception as e:
                                    await self.highrise.chat(f"Could not ban {r_user.username}: {e}")
                                return
                        await self.highrise.chat("User not found.")
                    except (ValueError, IndexError):
                        await self.highrise.chat("Usage: !ban @username [minutes]")
                    except Exception as e:
                        print(f"Ban error: {e}")

            elif message.startswith("!unban "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        target_name = parts[1].replace("@", "").lower()
                        # Try finding in banned list first
                        if target_name in self.banned_users:
                            target_id = self.banned_users[target_name]
                            try:
                                await self.highrise.moderate_room(target_id, "unban", 1)
                                await self.highrise.chat(f"✅ <#00FF00>@{target_name} has been unbanned!")
                                await self.report_moderation_action("Unban", target_name, user.username)
                            except Exception as e:
                                await self.highrise.chat(f"Could not unban {target_name}: {e}")
                        else:
                            await self.highrise.chat(f"Could not find {target_name} in local ban list.")
                    except IndexError:
                        await self.highrise.chat("Usage: !unban @username")
                    except Exception as e:
                        print(f"Unban error: {e}")

            elif message.startswith("!kick"):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        target_name = parts[1].replace("@", "").lower()
                        room_users = (await self.highrise.get_room_users()).content
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                await self.highrise.moderate_room(r_user.id, "kick")
                                await self.highrise.chat(f"👢 Kicked {r_user.username}")
                                await self.report_moderation_action("Kick", r_user.username, user.username)
                                break
                    except IndexError:
                        await self.highrise.chat("Usage: !kick @username")
                    return

            elif message.startswith("!void "):
                if user.username in self.OWNERS:
                    try:
                        parts = message.split()
                        if len(parts) < 2:
                            await self.highrise.chat("Usage: !void @username")
                            return
                        target_name = parts[1].replace("@", "").lower()
                        room_users = (await self.highrise.get_room_users()).content
                        target_id = None
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                target_id = r_user.id
                                break
                        
                        if target_id:
                            # Teleport to far away coordinates
                            void_pos = Position(1000, 1000, 1000, "FrontRight")
                            await self.highrise.teleport(target_id, void_pos)
                            await self.highrise.chat(f"🌌 <#FF0000>@{target_name} has been sent to the void! 🌀")
                        else:
                            await self.highrise.chat(f"❌ User @{target_name} not found.")
                    except Exception as e:
                        print(f"Void error: {e}")
                        await self.highrise.chat("Usage: !void @username")
                else:
                    await self.highrise.chat("❌ Owner only!")
                return

            elif message.startswith("!mute "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        target_name = parts[1].replace("@", "").lower()
                        minutes = int(parts[2]) if len(parts) > 2 else 5
                        duration = minutes * 60 
                        
                        room_users = (await self.highrise.get_room_users()).content
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                try:
                                    await self.highrise.moderate_room(r_user.id, "mute", duration)
                                    await self.highrise.chat(f"🔇 <#FF0000>@{r_user.username} has been muted for {minutes} minutes!")
                                    await self.report_moderation_action(f"Mute ({minutes}m)", r_user.username, user.username)
                                except Exception as e:
                                    await self.highrise.chat(f"Could not mute {r_user.username}: {e}")
                                return
                        await self.highrise.chat("User not found.")
                    except (ValueError, IndexError):
                        await self.highrise.chat("Usage: !mute @username [minutes]")
                    except Exception as e:
                        print(f"Mute error: {e}")

            elif message.startswith("!unmute "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "").lower()
                        room_users = (await self.highrise.get_room_users()).content
                        for r_user, _ in room_users:
                            if r_user.username.lower() == target_name:
                                try:
                                    await self.highrise.moderate_room(r_user.id, "mute", 1) # Mute for 1 second effectively unmutes or use 0
                                    # Note: Highrise API 'mute' with 0 usually un-mutes or just expires instantly.
                                    # Some implementations use "unmute" action if available, ensuring we try "mute" with 0 first.
                                    # The documentation says moderate_room action can be "kick", "ban", "unban", "mute".
                                    # To unmute, typically you just mute for 0 seconds or very short duration if specific un-mute isn't there.
                                    # However, `moderate_room` might NOT support explicit unmute. Let's try sending 1 second which is practically instant.
                                    await self.highrise.moderate_room(r_user.id, "mute", 1)
                                    await self.highrise.chat(f"🔊 <#00FF00>@{r_user.username} has been unmuted!")
                                    await self.report_moderation_action("Unmute", r_user.username, user.username)
                                except Exception as e:
                                    # If moderate_room action "unmute" exists we can try that too in catch
                                    await self.highrise.chat(f"Could not unmute {r_user.username}: {e}")
                                return
                        
                        # If user not in room, we can't unmute them easily via moderate_room usually requiring ID
                        await self.highrise.chat("User not found in room.")
                    except IndexError:
                        await self.highrise.chat("Usage: !unmute @username")
                    except Exception as e:
                        print(f"Unmute error: {e}")


            elif message.startswith("!vipcost"):
                parts = message.split()
                if len(parts) >= 3 and user.username in self.ADMINS:
                    try:
                        tier = parts[1].lower()
                        cost = int(parts[2])
                        
                        if "30day" in tier:
                            self.vip_cost_30d = cost
                            self.settings["vip_cost_30d"] = cost
                            await self.highrise.chat(f"💎 <#00FF00>30 Days VIP Cost set to <#FFFF00>{cost} Gold<#00FF00>!")
                        elif "90day" in tier:
                            self.vip_cost_90d = cost
                            self.settings["vip_cost_90d"] = cost
                            await self.highrise.chat(f"💎 <#00FF00>90 Days VIP Cost set to <#FFFF00>{cost} Gold<#00FF00>!")
                        elif "perm" in tier:
                            self.vip_cost_perm = cost
                            self.settings["vip_cost_perm"] = cost
                            await self.highrise.chat(f"💎 <#00FF00>Permanent VIP Cost set to <#FFFF00>{cost} Gold<#00FF00>!")
                        else:
                            await self.highrise.chat("<#FF0000>Invalid tier! Use: !vipcost 30days/90days/permanent [amount]")
                            return
                            
                        self.save_settings()
                    except (ValueError, IndexError):
                        await self.highrise.chat("<#FF0000>Usage: !vipcost [tier] [amount]")
                else:
                    await self.highrise.chat(
                        f"💎 **CURRENT VIP COSTS** 💎\n"
                        f"🔹 30 Days: {self.vip_cost_30d}G\n"
                        f"🔹 90 Days: {self.vip_cost_90d}G\n"
                        f"🔹 Permanent: {self.vip_cost_perm}G\n"
                        "To buy, tip the exact amount to the bot!"
                    )

            elif msg_lower.startswith("!buyvip"):
                parts = message.split()
                if len(parts) < 2:
                    current_balance = self.user_tips_ledger.get(user.username, 0)
                    prices = (
                        "💎 **VIP PURCHASE SYSTEM** 💎\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━\n"
                        f"<#FFFF00>How to Buy:\n"
                        f"1. Tip the bot the Gold first.\n"
                        f"2. Use <#FFFFFF>-buyvip [duration] <#FFFF00>to claim!\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━\n"
                        f"💰 Your Current Balance: <#00FF00>{current_balance} Gold\n\n"
                        f"🔹 30 Days: <#FFFFFF>{self.vip_cost_30d} Gold\n"
                        f"🔹 90 Days: <#FFFFFF>{self.vip_cost_90d} Gold\n"
                        f"🔹 Permanent: <#FFFFFF>{self.vip_cost_perm} Gold\n"
                        "<#800080>━━━━━━━━━━━━━━━━━━\n"
                        "💡 Example: -buyvip 30days"
                    )
                    await self.highrise.chat(prices)
                    return

                # Handle purchase claim
                duration = parts[1].lower()
                cost = 0
                days_to_add = 0
                label = ""

                if "30" in duration:
                    cost = self.vip_cost_30d
                    days_to_add = 30
                    label = "30 DAYS"
                elif "90" in duration:
                    cost = self.vip_cost_90d
                    days_to_add = 90
                    label = "90 DAYS"
                elif "perm" in duration:
                    cost = self.vip_cost_perm
                    days_to_add = -1
                    label = "PERMANENT"
                else:
                    await self.highrise.chat("❌ Invalid duration! Use: 30days, 90days, or perm")
                    return

                # Check ledger
                user_balance = self.user_tips_ledger.get(user.username, 0)
                if user_balance >= cost:
                    # Grant VIP
                    if isinstance(self.VIPS, list):
                        self.VIPS = {v: "permanent" for v in self.VIPS}
                    
                    if days_to_add == -1:
                        expiration = "permanent"
                    else:
                        expiration = int(time.time() + (days_to_add * 86400))
                    
                    self.VIPS[user.username] = expiration
                    self.user_tips_ledger[user.username] -= cost
                    
                    self.settings["vips"] = self.VIPS
                    self.settings["user_tips_ledger"] = self.user_tips_ledger
                    
                    # Add to history
                    if "vip_history" not in self.settings: self.settings["vip_history"] = []
                    if user.username not in self.settings["vip_history"]:
                        self.settings["vip_history"].append(user.username)
                        
                    self.save_settings()
                    
                    await self.highrise.chat(f"🎉 <#00FF00>SUCCESS! @{user.username}, you are now a {label} VIP! 💎✨")
                    try: await self.highrise.send_emote("emote-fabulous", user.id)
                    except: pass
                else:
                    needed = cost - user_balance
                    await self.highrise.chat(f"⚠️ <#FFFF00>Insufficient balance! You have {user_balance}G. You need {needed}G more for {label} VIP. Tip the bot first!")







            elif message.startswith("!follow "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        self.following_user = target_name
                        await self.highrise.chat(f"🚶‍♂️ <#00FF00>Now following @{target_name}!")
                    except IndexError:
                        await self.highrise.chat("Usage: !follow @username")

            elif msg_lower == "!unfollow":
                if user.username in self.ADMINS:
                    self.following_user = None
                    await self.highrise.chat("🛑 <#FFFF00>Stopped following.")
                else: 
                     await self.highrise.chat("<#FF0000>Admins only!")














            elif message.startswith("!flash"):
                parts = message.split()
                if len(parts) > 1:
                    arg = parts[1].lower()
                    if arg == "off":
                        if user.username in self.flash_users:
                            self.flash_users.remove(user.username)
                            if "flash_users" in self.settings and user.username in self.settings["flash_users"]:
                                self.settings["flash_users"].remove(user.username)
                                self.save_settings()
                            await self.highrise.chat(f"🚫 <#FF0000>Flash Mode DISABLED for @{user.username}.")
                        else:
                            await self.highrise.chat("⚠️ Flash Mode is already disabled.")
                        return
                
                # Default: Enable
                self.flash_users.add(user.username)
                
                # Make it persistent
                if "flash_users" not in self.settings:
                    self.settings["flash_users"] = []
                if user.username not in self.settings["flash_users"]:
                    self.settings["flash_users"].append(user.username)
                    self.save_settings()
                    
                await self.highrise.chat(f"⚡ <#FFFF00>Flash Mode ENABLED for @{user.username}! Click to teleport instantly! (Type !flash off to disable) ⚡")

            elif message.startswith("!dancefloor"):
                if user.username in self.OWNERS:
                    parts = message.split()
                    
                    # Show help if no arguments
                    if len(parts) == 1:
                        help_msg = (
                            "<#FF00FF>🕺 DANCE FLOOR SYSTEM 🕺\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#FFFF00>Usage:\n"
                            "<#FFFFFF>!dancefloor set - <#888888>Set corner 1 (stand where you want)\n"
                            "<#FFFFFF>!dancefloor set2 - <#888888>Set corner 2 (creates area)\n"
                            "<#FFFFFF>!dancefloor on - <#888888>Activate dance floor\n"
                            "<#FFFFFF>!dancefloor off - <#888888>Deactivate dance floor\n"
                            "<#FFFFFF>!dancefloor status - <#888888>Check current status\n"
                            "<#FFFFFF>!dancefloor clear - <#888888>Clear dance floor area\n"
                            "<#800080>━━━━━━━━━━━━━━━━━━━━━\n"
                            "<#00FFFF>ℹ️ When active, users in the area will auto-dance!"
                        )
                        await self.safe_chat(help_msg)
                        return
                    
                    subcmd = parts[1].lower()
                    
                    if subcmd == "set":
                        # Set first corner position
                        try:
                            room_users = (await self.highrise.get_room_users()).content
                            caller_pos = None
                            for u, pos in room_users:
                                if u.id == user.id:
                                    caller_pos = pos
                                    break
                            
                            if not caller_pos:
                                await self.highrise.chat("❌ Could not find your position!")
                                return
                            
                            if isinstance(caller_pos, AnchorPosition):
                                await self.highrise.chat("❌ Please stand on the floor, not on an anchor!")
                                return
                            
                            # Save first corner
                            self.dance_floor_area["min"] = caller_pos
                            await self.highrise.chat(f"✅ <#00FF00>Dance Floor Corner 1 set at ({caller_pos.x}, {caller_pos.y}, {caller_pos.z})!\n<#FFFF00>Now use !dancefloor set2 at the opposite corner.")
                        
                        except Exception as e:
                            print(f"Dance floor set error: {e}")
                            await self.highrise.chat("❌ Error setting dance floor corner 1!")
                    
                    elif subcmd == "set2":
                        # Set second corner position
                        try:
                            if self.dance_floor_area.get("min") is None:
                                await self.highrise.chat("❌ Please set corner 1 first with !dancefloor set")
                                return
                            
                            room_users = (await self.highrise.get_room_users()).content
                            caller_pos = None
                            for u, pos in room_users:
                                if u.id == user.id:
                                    caller_pos = pos
                                    break
                            
                            if not caller_pos:
                                await self.highrise.chat("❌ Could not find your position!")
                                return
                            
                            if isinstance(caller_pos, AnchorPosition):
                                await self.highrise.chat("❌ Please stand on the floor, not on an anchor!")
                                return
                            
                            # Save second corner
                            self.dance_floor_area["max"] = caller_pos
                            
                            # Calculate area size
                            min_pos = self.dance_floor_area["min"]
                            width = abs(caller_pos.x - min_pos.x)
                            height = abs(caller_pos.y - min_pos.y)
                            depth = abs(caller_pos.z - min_pos.z)
                            
                            await self.highrise.chat(
                                f"✅ <#00FF00>Dance Floor Corner 2 set!\n"
                                f"<#00FFFF>📐 Area Size: {width:.1f}x{height:.1f}x{depth:.1f}\n"
                                f"<#FFFF00>Use !dancefloor on to activate!"
                            )
                        
                        except Exception as e:
                            print(f"Dance floor set2 error: {e}")
                            await self.highrise.chat("❌ Error setting dance floor corner 2!")
                    
                    elif subcmd == "on":
                        # Activate dance floor
                        if self.dance_floor_area.get("min") is None or self.dance_floor_area.get("max") is None:
                            await self.highrise.chat("❌ Please set both corners first! Use !dancefloor set and !dancefloor set2")
                            return
                        
                        self.dance_floor_active = True
                        await self.highrise.chat("🎉 <#00FF00>DANCE FLOOR ACTIVATED! 🕺💃\n<#FFFF00>Step into the area to start dancing!")
                    
                    elif subcmd == "off":
                        # Deactivate dance floor
                        self.dance_floor_active = False
                        await self.highrise.chat("🛑 <#FF0000>Dance Floor DEACTIVATED!")
                    
                    elif subcmd == "status":
                        # Show status
                        status = "🟢 ACTIVE" if self.dance_floor_active else "🔴 INACTIVE"
                        area_set = "✅ SET" if (self.dance_floor_area.get("min") and self.dance_floor_area.get("max")) else "❌ NOT SET"
                        
                        status_msg = (
                            f"<#00FFFF>🕺 DANCE FLOOR STATUS 🕺\n"
                            f"<#FFFFFF>Status: {status}\n"
                            f"<#FFFFFF>Area: {area_set}"
                        )
                        
                        if self.dance_floor_area.get("min") and self.dance_floor_area.get("max"):
                            min_pos = self.dance_floor_area["min"]
                            max_pos = self.dance_floor_area["max"]
                            status_msg += (
                                f"\n<#FFFF00>Corner 1: ({min_pos.x:.1f}, {min_pos.y:.1f}, {min_pos.z:.1f})\n"
                                f"<#FFFF00>Corner 2: ({max_pos.x:.1f}, {max_pos.y:.1f}, {max_pos.z:.1f})"
                            )
                        
                        await self.safe_chat(status_msg)
                    
                    elif subcmd == "clear":
                        # Clear dance floor area
                        self.dance_floor_area = {"min": None, "max": None}
                        self.dance_floor_active = False
                        await self.highrise.chat("🗑️ <#FFFF00>Dance Floor area cleared and deactivated!")
                    
                    else:
                        await self.highrise.chat("❌ Unknown command! Use !dancefloor for help.")
                
                else:
                    await self.highrise.chat("<#FF0000>❌ Owners only!")
                return

            elif message.startswith("!savefit "):
                if user.username in self.OWNERS:
                    try:
                        parts = message.split()
                        if len(parts) < 2:
                            await self.highrise.chat("Usage: !savefit [name/number]")
                            return
                        
                        fit_name = parts[1]
                        
                        # Get Bot Outfit
                        outfit_response = await self.highrise.get_my_outfit()
                        
                        # Check for Error response
                        from highrise import Error
                        if isinstance(outfit_response, Error):
                             print(f"Error fetching outfit: {outfit_response}")
                             await self.highrise.chat(f"❌ Could not fetch outfit: {outfit_response.message}")
                             return

                        outfit = outfit_response.outfit
                        
                        # Serialize
                        serialized_outfit = []
                        for item in outfit:
                            # Safely get attributes with fallbacks
                            item_id = item.id if hasattr(item, 'id') else None
                            item_amount = item.amount if hasattr(item, 'amount') else 1
                            item_type = item.type if hasattr(item, 'type') else "clothing"
                            item_active_palette = item.active_palette if hasattr(item, 'active_palette') else -1
                            
                            if item_id:
                                item_dict = {
                                    "id": item_id,
                                    "amount": item_amount,
                                    "type": item_type,
                                    "active_palette": item_active_palette
                                }
                                serialized_outfit.append(item_dict)
                            
                        self.saved_outfits[fit_name] = serialized_outfit
                        self.settings["saved_outfits"] = self.saved_outfits
                        self.save_settings()
                        
                        await self.highrise.chat(f"💾 Outfit saved as '{fit_name}' with {len(serialized_outfit)} items! 👕")
                    except Exception as e:
                        print(f"Savefit error: {e}")
                        import traceback
                        traceback.print_exc()
                        await self.highrise.chat(f"❌ Error saving outfit: {str(e)[:100]}")
                else:
                    await self.highrise.chat("❌ Owner only!")

            elif message.startswith("!fit ") or message.startswith("!loadfit "):
                if user.username in self.OWNERS:
                    try:
                        parts = message.split()
                        if len(parts) < 2:
                            await self.highrise.chat("Usage: !fit [name/number] or !loadfit [name/number]")
                            return
                        
                        fit_name = parts[1]
                        
                        if fit_name in self.saved_outfits:
                            raw_outfit = self.saved_outfits[fit_name]
                            # Deserialize
                            outfit = []
                            for item_data in raw_outfit:
                                try:
                                    # Handle active_palette - it can be None, -1, or a valid palette number
                                    active_palette = item_data.get("active_palette")
                                    if active_palette is None:
                                        active_palette = -1
                                    
                                    outfit.append(Item(
                                        type=item_data.get("type", "clothing"),
                                        amount=item_data.get("amount", 1),
                                        id=item_data["id"],
                                        account_bound=False,
                                        active_palette=active_palette
                                    ))
                                except Exception as item_error:
                                    print(f"Error loading item {item_data.get('id', 'unknown')}: {item_error}")
                                    # Continue with other items even if one fails
                                    continue
                            
                            if outfit:
                                await self.highrise.set_outfit(outfit)
                                await self.highrise.chat(f"👕 Outfit '{fit_name}' loaded successfully! ✨")
                            else:
                                await self.highrise.chat(f"❌ Outfit '{fit_name}' has no valid items.")
                        else:
                             await self.highrise.chat(f"❌ Outfit '{fit_name}' not found. Use !fitlist to see saved outfits.")
                    except Exception as e:
                         print(f"Loadfit error: {e}")
                         import traceback
                         traceback.print_exc()
                         await self.highrise.chat(f"❌ Error loading outfit: {str(e)[:100]}")
                else:
                    await self.highrise.chat("❌ Owner only!")

            elif message.startswith("!removefit ") or message.startswith("!deletefit "):
                if user.username in self.OWNERS:
                    try:
                        fit_name = message.split()[1]
                        if fit_name in self.saved_outfits:
                            del self.saved_outfits[fit_name]
                            self.settings["saved_outfits"] = self.saved_outfits
                            self.save_settings()
                            await self.highrise.chat(f"🗑️ Outfit '{fit_name}' deleted!")
                        else:
                            await self.highrise.chat(f"❌ Outfit '{fit_name}' not found.")
                    except:
                        await self.highrise.chat("Usage: !removefit [name]")
                else:
                    await self.highrise.chat("❌ Owner only!")

            elif msg_lower == "!fitlist":
                if user.username in self.OWNERS:
                    if not self.saved_outfits:
                        await self.highrise.chat("❌ No saved outfits.")
                    else:
                        outfits = ", ".join(self.saved_outfits.keys())
                        await self.highrise.chat(f"👕 Saved Outfits: {outfits}")
                else:
                    await self.highrise.chat("❌ Owner only!")

            elif msg_lower == "!stopflash":
                if user.username in self.flash_users:
                    self.flash_users.remove(user.username)
                    
                    # Remove from persistent settings
                    if "flash_users" in self.settings:
                        if user.username in self.settings["flash_users"]:
                            self.settings["flash_users"].remove(user.username)
                            self.save_settings()
                            
                    await self.highrise.chat(f"🚫 <#FF0000>Flash Mode DISABLED for @{user.username}.")
                else:
                    await self.highrise.chat("⚠️ Flash Mode is already disabled.")




            elif message.startswith("!bottoggle "):
                # !bottoggle [name] -> Switches between running and offline
                if user.username in self.OWNERS:
                     try:
                        target_name = message.split(" ", 1)[1].strip()
                        if os.path.exists("bots_config.json"):
                            with open("bots_config.json", "r") as f:
                                bots = json.load(f)
                            
                            found = False
                            for b in bots:
                                if b["name"].lower() == target_name.lower():
                                    new_status = "offline" if b["status"] == "running" else "running"
                                    b["status"] = new_status
                                    found = True
                                    await self.highrise.chat(f"🤖 Bot '{target_name}' is now **{new_status.upper()}**. (Restart run.py to apply)")
                                    break
                            
                            if found:
                                with open("bots_config.json", "w") as f:
                                    json.dump(bots, f, indent=4)
                            else:
                                await self.highrise.chat(f"❌ Bot '{target_name}' not found in configuration.")
                     except:
                        await self.highrise.chat("Usage: !bottoggle [bot_name]")
                else: await self.highrise.chat("❌ Owner only!")
                return

            elif message.startswith("!botroom "):
                if user.username in self.OWNERS:
                    parts = message.split()
                    if len(parts) < 2:
                        await self.highrise.chat("💡 Usage: !botroom [room_link/id]")
                        return
                    
                    room_input = parts[1]
                    new_room_id = room_input
                    
                    # 1. Parse Room ID from various link formats
                    if "highrise.game/room/" in room_input:
                        new_room_id = room_input.split("highrise.game/room/")[-1].split("?")[0].split("&")[0]
                    elif "high.rs/room?id=" in room_input:
                        new_room_id = room_input.split("high.rs/room?id=")[-1].split("&")[0]
                    
                    # 2. Inform the user
                    await self.highrise.chat(f"🚀 <#00FF00>Switching to new room! ID: {new_room_id}\n🌀 The bot will join there in a few seconds... (Bypassing cooldown)")
                    await asyncio.sleep(0.5)
                    
                    # 3. Update Configuration Files
                    try:
                        token_clean = self.bot_token.strip() if self.bot_token else ""
                        
                        # Update bots_config.json ONLY if it already exists
                        if os.path.exists("bots_config.json"):
                            try:
                                with open("bots_config.json", "r") as f:
                                    bots_data = json.load(f)
                                
                                found = False
                                if isinstance(bots_data, list):
                                    for b in bots_data:
                                        if b.get("token", "").strip() == token_clean:
                                            b["room_id"] = new_room_id
                                            b["force_restart"] = True
                                            found = True
                                            break
                                    
                                    if found:
                                        with open("bots_config.json", "w") as f:
                                            json.dump(bots_data, f, indent=4)
                            except Exception as e:
                                print(f"Error updating bots_config: {e}")
                        
                        # Always update config.json (Fallback/Main)
                        if os.path.exists("config.json"):
                            try:
                                with open("config.json", "r") as f:
                                    main_conf = json.load(f)
                                
                                # Verify if this is the "Main_Bot" or matches the current token
                                main_token = (main_conf.get("bot_token") or main_conf.get("token") or "").strip()
                                
                                # Update if tokens match or if bots_config doesn't exist (assuming single bot mode)
                                if main_token == token_clean or not os.path.exists("bots_config.json"):
                                    main_conf["room_id"] = new_room_id
                                    main_conf["force_restart"] = True # Manager now supports this in config.json
                                    with open("config.json", "w") as f:
                                        json.dump(main_conf, f, indent=4)
                            except Exception as e:
                                print(f"Error updating config.json: {e}")
                                
                        # 4. Exit to trigger manager restart
                        self.is_shutting_down = True
                        await asyncio.sleep(1) # Give a moment for tasks to see the flag
                        sys.exit(0)
                        
                    except Exception as e:
                        print(f"[ERROR] !botroom error: {e}")
                        await self.highrise.chat(f"❌ <#FF0000>Failed to update room configuration: {e}")
                else: 
                    await self.highrise.chat("❌ Owner only!")
                return

            elif msg_lower == "!roominfo":
                try:
                    room_id = self.room_id
                    room_link = f"https://highrise.game/room/{room_id}"
                    info_msg = (
                        f"<#00FFFF>📍 Room Information:\n"
                        f"<#FFFF00>🏠 Name: <#FF69B4>{self.current_room_name}\n"
                        f"<#FFFF00>🆔 ID: <#FFFFFF>{room_id}\n"
                        f"<#FFFF00>🔗 Link: <#FFFFFF>{room_link}"
                    )
                    await self.safe_chat(info_msg)
                except Exception as e:
                    print(f"Room info error: {e}")
                    await self.highrise.chat("<#FF0000>Error getting room info.")






            elif msg_lower == "!allemotes":
                if user.username in self.ADMINS:
                    self.playing_all_emotes = True
                    self.dance_floor_mode = False
                    await self.highrise.chat("🔄 Starting continuous ALL-emote loop! 🎭")
                else:
                    await self.highrise.chat("❌ Admin only!")

            elif msg_lower == "!dance":
                if user.username in self.ADMINS:
                    self.playing_all_emotes = True
                    self.dance_floor_mode = True
                    await self.highrise.chat("💃 Dance Floor Mode ACTIVATED! Let's party! 🕺")
                else:
                    await self.highrise.chat("❌ Admin only!")

            elif msg_lower == "!stopemotes" or msg_lower == "!stopdance":
                if user.username in self.ADMINS:
                    self.playing_all_emotes = False
                    self.dance_floor_mode = False
                    await self.highrise.chat("🛑 Stopped emote loop.")
                else:
                    await self.highrise.chat("❌ Admin only!")

            elif msg_lower in ["نوم", "نوم1"]:
                self.looping_users[user.id] = "idle_layingdown2"
                await self.highrise.send_emote("idle_layingdown2", user.id)

            elif msg_lower == "نوم2":
                self.looping_users[user.id] = "idle_layingdown"
                await self.highrise.send_emote("idle_layingdown", user.id)

            elif msg_lower == "نوم3":
                self.looping_users[user.id] = "emote-sleepy"
                await self.highrise.send_emote("emote-sleepy", user.id)

            elif msg_lower == "نوم4":
                self.looping_users[user.id] = "emote-tired"
                await self.highrise.send_emote("emote-tired", user.id)

            elif msg_lower == "نوم5":
                self.looping_users[user.id] = "emote-yawn"
                await self.highrise.send_emote("emote-yawn", user.id)

            elif msg_lower == "نوم6":
                self.looping_users[user.id] = "emote-sleep"
                await self.highrise.send_emote("emote-sleep", user.id)

            elif msg_lower == "ست":
                self.looping_users[user.id] = "sit-open"
                await self.highrise.send_emote("sit-open", user.id)

            elif msg_lower == "جوست":
                self.looping_users[user.id] = "emote-ghost-idle"
                await self.highrise.send_emote("emote-ghost-idle", user.id)

            # ===== USER COMMANDS (Arabic) =====

            elif message.startswith("وديني "):
                try:
                    target_name = message.split()[1].replace("@", "")
                    room_users = (await self.highrise.get_room_users()).content
                    target_pos = None
                    for u, p in room_users:
                        if u.username.lower() == target_name.lower() and isinstance(p, Position):
                            target_pos = p
                            break
                    if target_pos:
                        await self.highrise.teleport(user.id, Position(target_pos.x + 0.5, target_pos.y, target_pos.z, "FrontRight"))
                    else:
                        await self.highrise.chat(f"❌ @{user.username} ما لقيتش @{target_name} في الروم")
                except Exception as e:
                    await self.highrise.chat("الاستخدام: وديني @اسم")

            elif msg_lower == "توقف":
                if user.id in self.looping_users:
                    self.looping_users.pop(user.id, None)
                    await self.highrise.chat(f"🛑 @{user.username} وقف!")

            elif msg_lower in ["متوسط", "وسط"]:
                try:
                    saved = self.settings.get("saved_positions", {})
                    sp = saved.get(msg_lower) or saved.get("وسط") or saved.get("متوسط")
                    if sp:
                        await self.highrise.teleport(user.id, Position(sp["x"], sp["y"], sp["z"], sp.get("facing", "FrontRight")))
                    else:
                        await self.highrise.teleport(user.id, Position(0, 0, 0, "FrontRight"))
                except Exception as e:
                    print(f"Teleport center error: {e}")

            elif msg_lower in ["طلعني", "فوق"]:
                try:
                    saved = self.settings.get("saved_positions", {})
                    sp = saved.get(msg_lower) or saved.get("فوق") or saved.get("طلعني")
                    if sp:
                        await self.highrise.teleport(user.id, Position(sp["x"], sp["y"], sp["z"], sp.get("facing", "FrontRight")))
                    else:
                        room_users = (await self.highrise.get_room_users()).content
                        for u, p in room_users:
                            if u.id == user.id and isinstance(p, Position):
                                new_y = min(p.y + 1.0, 20.0)
                                await self.highrise.teleport(user.id, Position(p.x, new_y, p.z, p.facing))
                                break
                except Exception as e:
                    print(f"Move up error: {e}")

            elif msg_lower in ["نزلني", "تحت"]:
                try:
                    saved = self.settings.get("saved_positions", {})
                    sp = saved.get(msg_lower) or saved.get("نزلني") or saved.get("تحت")
                    if sp:
                        await self.highrise.teleport(user.id, Position(sp["x"], sp["y"], sp["z"], sp.get("facing", "FrontRight")))
                    else:
                        room_users = (await self.highrise.get_room_users()).content
                        for u, p in room_users:
                            if u.id == user.id and isinstance(p, Position):
                                new_y = max(p.y - 1.0, 0.0)
                                await self.highrise.teleport(user.id, Position(p.x, new_y, p.z, p.facing))
                                break
                except Exception as e:
                    print(f"Move down error: {e}")

            elif msg_lower == "vip":
                saved = self.settings.get("saved_positions", {})
                sp = saved.get("vip")
                if sp:
                    await self.highrise.teleport(user.id, Position(sp["x"], sp["y"], sp["z"], sp.get("facing", "FrontRight")))
                else:
                    if user.id in self.vip_users:
                        await self.highrise.chat(f"✅ @{user.username} أنت VIP! 💎")
                    else:
                        cost = self.settings.get("vip_cost", 100)
                        await self.highrise.chat(f"❌ @{user.username} مش VIP. اشتري VIP بـ {cost} جولد!")

            # رقصة برقم - any number 1-235
            elif msg_lower.isdigit():
                import json as _json
                try:
                    with open("data/bot_emotes_full.json", "r", encoding="utf-8") as _f:
                        _emotes = _json.load(_f)
                    emote_data = _emotes.get(msg_lower)
                    if emote_data:
                        eid = emote_data["id"]
                        self.looping_users[user.id] = eid
                        await self.highrise.send_emote(eid, user.id)
                except Exception as e:
                    print(f"Number emote error: {e}")

            # ===== ADMIN COMMANDS (Arabic) =====

            elif msg_lower == "حفظ" or message.startswith("حفظ "):
                if user.username in self.ADMINS:
                    parts = message.strip().split(maxsplit=1)
                    keyword = parts[1].strip().lower() if len(parts) > 1 else None
                    VALID_KEYWORDS = ["فوق", "وسط", "تحت", "vip", "نزلني", "متوسط", "طلعني"]
                    room_users = (await self.highrise.get_room_users()).content
                    for target, pos in room_users:
                        if target.id == user.id and isinstance(pos, Position):
                            if keyword and keyword in VALID_KEYWORDS:
                                if "saved_positions" not in self.settings:
                                    self.settings["saved_positions"] = {}
                                self.settings["saved_positions"][keyword] = {"x": pos.x, "y": pos.y, "z": pos.z, "facing": pos.facing}
                                self.save_settings()
                                await self.highrise.chat(f"✅ تم حفظ موقع [{keyword}] عند ({pos.x:.1f}, {pos.y:.1f}, {pos.z:.1f})")
                            else:
                                self.settings["perm_pos"] = {"x": pos.x, "y": pos.y, "z": pos.z, "facing": pos.facing}
                                self.save_settings()
                                await self.highrise.chat(f"✅ تم حفظ موقع البوت!")
                            break
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            elif msg_lower == "اذهب":
                if user.username in self.ADMINS:
                    if "perm_pos" in self.settings:
                        p = self.settings["perm_pos"]
                        await self.highrise.walk_to(Position(p["x"], p["y"], p["z"], p.get("facing", "FrontRight")))
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            elif message.startswith("جيب "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        bot_pos = None
                        target_id = None
                        for u, p in room_users:
                            if u.id == self.bot_id and isinstance(p, Position):
                                bot_pos = p
                            if u.username.lower() == target_name.lower():
                                target_id = u.id
                        if target_id and bot_pos:
                            await self.highrise.teleport(target_id, Position(bot_pos.x + 0.5, bot_pos.y, bot_pos.z, "FrontRight"))
                            await self.highrise.chat(f"✅ تم جلب @{target_name}!")
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name}")
                    except Exception as e:
                        await self.highrise.chat("الاستخدام: جيب @اسم")
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            elif message.startswith("بدل "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        name1 = parts[1].replace("@", "")
                        name2 = parts[2].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        u1_id, u1_pos, u2_id, u2_pos = None, None, None, None
                        for u, p in room_users:
                            if u.username.lower() == name1.lower() and isinstance(p, Position):
                                u1_id, u1_pos = u.id, p
                            if u.username.lower() == name2.lower() and isinstance(p, Position):
                                u2_id, u2_pos = u.id, p
                        if u1_id and u2_id and u1_pos and u2_pos:
                            await self.highrise.teleport(u1_id, Position(u2_pos.x, u2_pos.y, u2_pos.z, u2_pos.facing))
                            await self.highrise.teleport(u2_id, Position(u1_pos.x, u1_pos.y, u1_pos.z, u1_pos.facing))
                            await self.highrise.chat(f"🔄 تم تبديل @{name1} و @{name2}!")
                        else:
                            await self.highrise.chat("❌ ما لقيتش اليوزرات")
                    except Exception as e:
                        await self.highrise.chat("الاستخدام: بدل @اسم1 @اسم2")
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            elif message.startswith("ثبت "):
                if user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        target_id, target_pos = None, None
                        for u, p in room_users:
                            if u.username.lower() == target_name.lower() and isinstance(p, Position):
                                target_id, target_pos = u.id, p
                                break
                        if target_id and target_pos:
                            self.frozen_users[target_id] = target_pos
                            await self.highrise.chat(f"🧊 تم تجميد @{target_name}!")
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name}")
                    except Exception as e:
                        await self.highrise.chat("الاستخدام: ثبت @اسم")
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            elif message.strip().startswith("الغ ثبت"):
                if user.username in self.ADMINS:
                    try:
                        parts = message.strip().split()
                        if len(parts) < 2:
                            await self.highrise.chat("❌ الاستخدام: الغ ثبت @اسم")
                        else:
                            target_name = parts[1].lstrip("@") if len(parts) > 1 else ""
                            room_users = (await self.highrise.get_room_users()).content
                            target_id = None
                            for u, p in room_users:
                                if u.username.lower() == target_name.lower():
                                    target_id = u.id
                                    break
                            if not target_id:
                                await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                            elif target_id in self.frozen_users:
                                del self.frozen_users[target_id]
                                await self.highrise.chat(f"✅ تم إلغاء تثبيت @{target_name}!")
                            else:
                                await self.highrise.chat(f"⚠️ @{target_name} مش متثبت أصلاً")
                    except Exception as e:
                        print(f"الغ ثبت command error: {e}")
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            elif message.startswith("/a ") and message.split()[1].isdigit():
                if user.username in self.ADMINS:
                    try:
                        import json as _json
                        num = message.split()[1]
                        with open("data/bot_emotes_full.json", "r", encoding="utf-8") as _f:
                            _emotes = _json.load(_f)
                        emote_data = _emotes.get(num)
                        if emote_data:
                            eid = emote_data["id"]
                            self.looping_users[user.id] = eid
                            await self.highrise.send_emote(eid, user.id)
                        else:
                            await self.highrise.chat(f"❌ رقم مش موجود")
                    except Exception as e:
                        print(f"/a command error: {e}")
                else:
                    await self.highrise.chat("❌ أدمن بس!")

            # ===== VIP COMMANDS (Arabic) =====

            elif msg_lower == "wink":
                if user.id in self.vip_users or user.username in self.ADMINS:
                    await self.highrise.send_emote("emote-kiss", user.id)
                else:
                    await self.highrise.chat(f"❌ @{user.username} الأمر ده لـ VIP بس!")

            elif msg_lower.startswith("follow ") or msg_lower.startswith("follow@"):
                if user.id in self.vip_users or user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        self.following_user = target_name
                        await self.highrise.chat(f"🚶 البوت بيتبع @{target_name}!")
                    except:
                        await self.highrise.chat("الاستخدام: follow @اسم")
                else:
                    await self.highrise.chat(f"❌ @{user.username} الأمر ده لـ VIP بس!")

            elif msg_lower == "stopfollow":
                if user.id in self.vip_users or user.username in self.ADMINS:
                    self.following_user = None
                    await self.highrise.chat("🛑 البوت وقف المتابعة!")
                else:
                    await self.highrise.chat(f"❌ @{user.username} الأمر ده لـ VIP بس!")

            elif msg_lower.startswith("lead ") or msg_lower.startswith("lead@"):
                if user.id in self.vip_users or user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        user_pos = None
                        target_id = None
                        for u, p in room_users:
                            if u.id == user.id and isinstance(p, Position):
                                user_pos = p
                            if u.username.lower() == target_name.lower():
                                target_id = u.id
                        if target_id and user_pos:
                            await self.highrise.teleport(target_id, Position(user_pos.x + 0.5, user_pos.y, user_pos.z, "FrontRight"))
                            await self.highrise.chat(f"✅ تم جلب @{target_name} عندك!")
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name}")
                    except:
                        await self.highrise.chat("الاستخدام: lead @اسم")
                else:
                    await self.highrise.chat(f"❌ @{user.username} الأمر ده لـ VIP بس!")

            elif msg_lower == "vip":
                if user.id in self.vip_users:
                    await self.highrise.chat(f"✅ @{user.username} أنت VIP! 💎")
                else:
                    cost = self.settings.get("vip_cost", 100)
                    await self.highrise.chat(f"❌ @{user.username} مش VIP. اشتري VIP بـ {cost} جولد!")

            elif message.startswith("عقاب "):
                if user.id in self.vip_users or user.username in self.ADMINS:
                    try:
                        target_name = message.split()[1].replace("@", "")
                        room_users = (await self.highrise.get_room_users()).content
                        target_id = None
                        for u, _ in room_users:
                            if u.username.lower() == target_name.lower():
                                target_id = u.id
                                break
                        if target_id:
                            await self.highrise.send_emote("emote-rofl", target_id)
                            await self.highrise.chat(f"😂 @{target_name} اتعاقب! 🎉")
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name}")
                    except:
                        await self.highrise.chat("الاستخدام: عقاب @اسم")
                else:
                    await self.highrise.chat(f"❌ @{user.username} الأمر ده لـ VIP بس!")


            elif message.lower().startswith("!teles "):
                if user.username in self.ADMINS:
                    try:
                        parts = message.split()
                        if len(parts) >= 3:
                            # !teles @user location
                            target_user_name = parts[1].replace("@", "")
                            location_name = " ".join(parts[2:]).lower()
                            
                            if location_name in self.locations:
                                loc_data = self.locations[location_name]
                                
                                # Find user ID
                                room_users = (await self.highrise.get_room_users()).content
                                target_id = None
                                target_username_case = ""
                                for r_user, _ in room_users:
                                    if r_user.username.lower() == target_user_name.lower():
                                        target_id = r_user.id
                                        target_username_case = r_user.username
                                        break
                                
                                if target_id:
                                    try:
                                        position = Position(loc_data["x"], loc_data["y"], loc_data["z"], loc_data.get("facing", "FrontRight"))
                                        await self.highrise.teleport(target_id, position)
                                        await self.highrise.chat(f"🌀 Teleported @{target_username_case} to '{location_name}'! ✨")
                                    except Exception as e:
                                        await self.highrise.chat(f"❌ Error teleporting: {e}")
                                else:
                                    await self.highrise.chat(f"❌ User @{target_user_name} not found in room.")
                            else:
                                await self.highrise.chat(f"❌ Location '{location_name}' not found. Use !telelist.")
                        else:
                            await self.highrise.chat("Usage: !teles @username [location_name]")
                    except Exception as e:
                        print(f"Tele command error: {e}")
                        await self.highrise.chat("❌ Error executing command.")
                else:
                    await self.highrise.chat("❌ Admin only!")

            elif msg_lower.startswith("!setdancefloor") or msg_lower.startswith("!dancefloor"):
                if user.username in self.ADMINS:
                    parts = message.split()
                    if len(parts) < 2:
                        await self.highrise.chat("Usage: !setdancefloor [1/2/clear] OR !dancefloor [on/off]")
                        return
                    
                    arg = parts[1].lower()
                    
                    if arg == "1":
                        # Get user position
                        room_users = (await self.highrise.get_room_users()).content
                        for u, p in room_users:
                            if u.id == user.id:
                                if isinstance(p, Position):
                                    self.dance_floor_area["min"] = p
                                    await self.highrise.chat(f"💃 <#00FF00>Dance Floor Corner 1 set to ({p.x}, {p.y}, {p.z})!")
                                else:
                                    await self.highrise.chat("❌ You must be on the floor (not furniture) to set a corner.")
                                break
                    
                    elif arg == "2":
                        # Get user position
                        room_users = (await self.highrise.get_room_users()).content
                        for u, p in room_users:
                            if u.id == user.id:
                                if isinstance(p, Position):
                                    self.dance_floor_area["max"] = p
                                    await self.highrise.chat(f"💃 <#00FF00>Dance Floor Corner 2 set to ({p.x}, {p.y}, {p.z})!")
                                else:
                                    await self.highrise.chat("❌ You must be on the floor (not furniture) to set a corner.")
                                break
                                
                    elif arg == "clear":
                        self.dance_floor_area = {"min": None, "max": None}
                        await self.highrise.chat("💃 <#00FF00>Dance Floor Area CLEARED! (Now covers entire room if enabled)")
                        
                    elif arg == "on":
                        self.dance_floor_active = True
                        if self.dance_floor_area["min"] and self.dance_floor_area["max"]:
                            await self.highrise.chat("💃 <#00FF00>Dance Floor ACTIVATED in defined area! Users will dance automatically! 🕺")
                        else:
                            await self.highrise.chat("💃 <#00FF00>Dance Floor ACTIVATED globally! THE WHOLE ROOM IS DANCING! 🕺")
                            
                    elif arg == "off":
                        self.dance_floor_active = False
                        await self.highrise.chat("🛑 <#FF0000>Dance Floor DEACTIVATED.")
                        
                else:
                    await self.highrise.chat("❌ Admin only!")








            # --- !id COMMAND ---
            elif msg_lower == "!id":
                await self.highrise.chat(f"🆔 @{user.username} الـ ID بتاعك هو: {user.id}")

            # --- كف COMMAND ---
            elif message.strip().startswith("كف "):
                try:
                    parts = message.strip().split()
                    if len(parts) < 2:
                        await self.highrise.chat("❌ الاستخدام: كف @اسم")
                        return
                    target_name = parts[1].lstrip("@")
                    room_users = (await self.highrise.get_room_users()).content
                    target_id = None
                    caller_pos = None
                    for u, p in room_users:
                        if u.username.lower() == target_name.lower():
                            target_id = u.id
                        if u.id == user.id and isinstance(p, Position):
                            caller_pos = p
                    if not target_id:
                        await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                        return
                    if caller_pos:
                        tp = Position(caller_pos.x + 0.5, caller_pos.y, caller_pos.z, "FrontRight")
                        await self.highrise.teleport(target_id, tp)
                    await asyncio.sleep(0.5)
                    await self.highrise.send_emote("emote-penstance", target_id)
                    await asyncio.sleep(4)
                    await self.highrise.send_emote("emote-deathdrop", target_id)
                except Exception as e:
                    print(f"كف command error: {e}")

            # --- k COMMAND: 20 سقفه ---
            elif msg_lower.startswith("k "):
                parts = message.strip().split()
                if len(parts) >= 2:
                    target_name = parts[1].lstrip("@")
                    try:
                        room_users = (await self.highrise.get_room_users()).content
                        target_user = None
                        for u, _ in room_users:
                            if u.username.lower() == target_name.lower():
                                target_user = u
                                break
                        if target_user:
                            for _ in range(20):
                                try:
                                    await self.highrise.react("clap", target_user.id)
                                    await asyncio.sleep(0.3)
                                except:
                                    pass
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                    except Exception as e:
                        print(f"k command error: {e}")

            # --- p COMMAND: 20 بوسه ---
            elif msg_lower.startswith("p "):
                parts = message.strip().split()
                if len(parts) >= 2:
                    target_name = parts[1].lstrip("@")
                    try:
                        room_users = (await self.highrise.get_room_users()).content
                        target_user = None
                        for u, _ in room_users:
                            if u.username.lower() == target_name.lower():
                                target_user = u
                                break
                        if target_user:
                            for _ in range(20):
                                try:
                                    await self.highrise.react("heart", target_user.id)
                                    await asyncio.sleep(0.3)
                                except:
                                    pass
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                    except Exception as e:
                        print(f"p command error: {e}")

            # --- g COMMAND: 20 غمزه ---
            elif msg_lower.startswith("g "):
                parts = message.strip().split()
                if len(parts) >= 2:
                    target_name = parts[1].lstrip("@")
                    try:
                        room_users = (await self.highrise.get_room_users()).content
                        target_user = None
                        for u, _ in room_users:
                            if u.username.lower() == target_name.lower():
                                target_user = u
                                break
                        if target_user:
                            for _ in range(20):
                                try:
                                    await self.highrise.react("wink", target_user.id)
                                    await asyncio.sleep(0.3)
                                except:
                                    pass
                        else:
                            await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                    except Exception as e:
                        print(f"g command error: {e}")

            # --- هات COMMAND (Owner/Admin only) ---
            elif message.strip().startswith("هات "):
                if user.username not in self.OWNERS and user.username not in self.ADMINS:
                    await self.highrise.chat("❌ هذا الأمر للمالك والمشرفين فقط!")
                else:
                    try:
                        parts = message.strip().split()
                        if len(parts) < 2:
                            await self.highrise.chat("❌ الاستخدام: هات @اسم")
                        else:
                            target_name = parts[1].lstrip("@")
                            room_users = (await self.highrise.get_room_users()).content
                            target_id = None
                            caller_pos = None
                            for u, p in room_users:
                                if u.username.lower() == target_name.lower():
                                    target_id = u.id
                                if u.id == user.id and isinstance(p, Position):
                                    caller_pos = p
                            if not target_id:
                                await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                            elif not caller_pos:
                                await self.highrise.chat("❌ ما قدرتش أحدد مكانك")
                            else:
                                tp = Position(caller_pos.x + 1.0, caller_pos.y, caller_pos.z, "FrontRight")
                                await self.highrise.teleport(target_id, tp)
                                await self.highrise.chat(f"<#00FF7F>✅ تم جلب @{target_name} عندك!")
                    except Exception as e:
                        print(f"هات command error: {e}")

            # --- زباله COMMAND (Owner/Admin only) ---
            elif message.strip().startswith("زباله "):
                if user.username not in self.OWNERS and user.username not in self.ADMINS:
                    await self.highrise.chat("❌ هذا الأمر للمالك والمشرفين فقط!")
                else:
                    try:
                        parts = message.strip().split()
                        if len(parts) < 2:
                            await self.highrise.chat("❌ الاستخدام: زباله @اسم")
                        else:
                            target_name = parts[1].lstrip("@")
                            room_users = (await self.highrise.get_room_users()).content
                            target_id = None
                            for u, p in room_users:
                                if u.username.lower() == target_name.lower():
                                    target_id = u.id
                                    break
                            if not target_id:
                                await self.highrise.chat(f"❌ ما لقيتش @{target_name} في الروم")
                            else:
                                fog_pos = Position(0.5, 0.0, 0.5, "FrontRight")
                                await self.highrise.teleport(target_id, fog_pos)
                                await self.highrise.chat(f"<#888888>🗑️ @{target_name} اتبعت للضباب! 😂")
                    except Exception as e:
                        print(f"زباله command error: {e}")

            # Check for location teleport
            elif msg_lower in self.locations:
                loc_data = self.locations[msg_lower]
                # Check if it's restricted
                if loc_data.get("owner_only", False):
                    if user.username not in self.OWNERS:
                        await self.highrise.chat(f"<#FF0000>❌ '{msg_lower}' is an OWNER-only location! 🔑")
                        return
                
                if loc_data.get("mod_only", False):
                    if user.username not in self.ADMINS:
                        await self.highrise.chat(f"<#FF0000>❌ '{msg_lower}' is a MOD-only location! 🛡️")
                        return

                if loc_data.get("vip_only", False):
                    if user.username not in self.VIPS and user.username not in self.ADMINS:
                        await self.highrise.chat(f"<#FF0000>❌ '{msg_lower}' is a VIP-only location! 👑")
                        return
                # Teleport user to location
                try:
                    position = Position(loc_data["x"], loc_data["y"], loc_data["z"], loc_data.get("facing", "FrontRight"))
                    await self.highrise.teleport(user.id, position)
                    await self.highrise.chat(f"<#00FFFF>🌀 Teleporting @{user.username} to '{msg_lower}'! ✨")
                except Exception as e:
                    print(f"Location teleport error: {e}")
                    await self.highrise.chat(f"<#FF0000>❌ Error teleporting to '{msg_lower}'.")

        except Exception as e:
            # Silent on disconnects
            if "closing transport" not in str(e).lower() and "connected" not in str(e).lower():
                print(f"Global on_chat error: {e}")

    async def on_user_leave(self, user):
        print(f"{user.username} left the room")
        self.looping_users.pop(user.id, None) # Stop loop if user leaves
        # Remove from ADMINS only if they were auto-detected (not manually added)
        if user.username.lower() in self.room_moderators:
            saved_admins = self.settings.get("admins", [])
            if user.username not in saved_admins and user.username not in self.OWNERS and user.username not in self.hardcoded_owners:
                try:
                    self.ADMINS.remove(user.username)
                except ValueError:
                    pass
            self.room_moderators.discard(user.username.lower())
        
        # Room Leave Message
        try:
            farewell_messages = [
                # 🇪🇬 مصرية
                f"<#FF6347>👋 @{user.username} مشي! الروم هيفتقده... أو مش هيفتقده 😂",
                f"<#FF69B4>😂 @{user.username} راح! يا خسارة... ما هو كان مضيف على فكرة 🤣",
                f"<#FFD700>🚪 @{user.username} سلّم وراح! متأخرش عليناا 😜",
                f"<#00FF7F>😹 @{user.username} خرج! الروم اتهدأ دلوقتي 😂",
                f"<#1E90FF>🤦 يسلم @{user.username}! عقبال ما نشوفك تاني... لو حصل 😂",
                f"<#DA70D6>😎 @{user.username} غادر! يا فرحتنا... يعني يا حسرتنا! 😅",
                f"<#FF4500>🎭 @{user.username} مشي! الدراما خلصت معاه 🍿",
                f"<#ADFF2F>😂 @{user.username} راح! كنا بنستناه يروح من زمان 😹",
                f"<#00CED1>👋 @{user.username} خرج! الباب مفتوح تعالى امتى ما حبيت 😂",
                f"<#FF1493>😆 @{user.username} سلّم! يا ترى هيرجع ولا هيتجنبنا؟ 🤔",
                f"<#7B68EE>🥹 @{user.username} راح! ربنا يوديه بالسلامة وميرجعش بدري 😂",
                f"<#32CD32>😂 @{user.username} مشي! الروم أهدأ شوية دلوقتي 😌",
                f"<#FFA500>🚶 @{user.username} مشي! اللي وراه يتصرف 😂",
                f"<#FF6347>😹 @{user.username} خرج! أخيراً... آه يعني آه 🤣",
                f"<#FFD700>👋 @{user.username} راح بسلامته! الروم كان شرف بوجوده... أو مش شرف مش متأكد 😂",
                # 🇸🇦 سعودية
                f"<#00D4FF>👋 @{user.username} ودّعنا! الله يسلّمه وإن شاء الله يرجع بخير 😂",
                f"<#FFD700>😂 @{user.username} مشى! يا ليت راح للأبد... يعني نتمنى يرجع بس 😜",
                f"<#FF4500>🚪 @{user.username} طلع! الله يكون في عونه من الروم الثاني 😂",
                f"<#7B68EE>😹 @{user.username} قام مشى! كنا نتمنى يطول بس ما قدر 🤣",
                f"<#32CD32>😎 @{user.username} ودّعنا! حياك الله وين ما رحت 👋",
                f"<#FF69B4>🤦 @{user.username} طار! الله يحفظه من نفسه أول 😂",
                f"<#FFA500>😆 @{user.username} سلّم وراح! الكل يقول وداعاً وهو ما يسمع 😂",
                f"<#1E90FF>🥹 @{user.username} قفل الباب وراه! ما عنده أدب يقول مع السلامة 😂",
                f"<#DA70D6>😂 @{user.username} مشى! نتمنى له رحلة ممتعة من الروم لـ... اللي بعده 🤣",
                f"<#00CED1>👋 @{user.username} ودّعنا! الله يسلّمه وياليت ما رجع بكره 😂",
                f"<#ADFF2F>😹 @{user.username} طلع! الروم بقى أوسع شوي دلوقتي 😜",
                f"<#FF1493>🚶 @{user.username} راح! وربنا يعوّض الروم بأحسن منه 😂",
                f"<#FF6347>😎 @{user.username} مشى وسلّم! الباب مفتوح لو رجعت 🚪",
                f"<#FFD700>🤣 @{user.username} طار من الروم! الله يصبّر الكل عليه 😂",
                f"<#00FF7F>👋 @{user.username} مشى! ما ودّع بس نودّعه احنا 😂",
            ]
            leave_msg = random.choice(farewell_messages)
            await self.highrise.chat(leave_msg)
        except Exception as e:
            print(f"Leave message error: {e}")

        # Time Tracking Session End
        if user.id in self.join_times:
            duration = int(time.time() - self.join_times.pop(user.id))
            if user.username not in self.user_times: self.user_times[user.username] = 0
            self.user_times[user.username] += duration
            self.settings["user_times"] = self.user_times
            
            # Update last_seen in user_stats
            if user.username not in self.user_stats: self.user_stats[user.username] = {}
            self.user_stats[user.username]["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.settings["user_stats"] = self.user_stats
            
            self.save_settings()
            print(f"Tracked {duration}s for {user.username}")



    async def on_unban(self, user):
       pass

    async def on_user_move(self, user, destination):
        """Handle movement for flash mode and freeze mode."""
        
        # Check if user is frozen
        if hasattr(self, 'frozen_users') and user.id in self.frozen_users:
            try:
                # Teleport back to frozen position
                await self.highrise.teleport(user.id, self.frozen_users[user.id])
            except Exception as e:
                print(f"Freeze teleport error: {e}")
            return # Stop processing

        # Flash Mode Logic
        if hasattr(self, 'flash_users') and user.username in self.flash_users:
             try:
                 await self.highrise.teleport(user.id, destination)
             except Exception as e:
                 print(f"Flash teleport error: {e}")



    async def on_emote(self, user, emote_id, receiver):
        # Ignore bot's own emotes
        if user.id == self.bot_id:
            return

        print(f"DEBUG: {user.username} used emote ID: {emote_id}")
        # Find emote name from ID
        found_name = None
        for name, eid in self.emotes.items():
            if eid == emote_id:
                found_name = name
                break
        
        if found_name:
            try:
                idx = self.emote_list.index(found_name) + 1
                await self.highrise.chat(f"✨ @{user.username} is looping {found_name}! 🎭")
                # Start looping for user
                self.looping_users[user.id] = emote_id
            except: pass

        # If someone waves at the bot, wave back!
        if receiver and receiver.id == self.bot_id:
            if "wave" in emote_id.lower():
                await self.highrise.send_emote("emote-wave", user.id)
                print(f"Waved back to {user.username}")

    async def on_tip(self, sender, receiver, tip):
        if receiver.id == self.bot_id:
            amount = tip.amount
            self.total_tips += amount
            
            # Record in history
            tip_entry = {
                "username": sender.username,
                "amount": amount,
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.tip_history.append(tip_entry)
            # Keep history at a reasonable size (last 50)
            if len(self.tip_history) > 50:
                self.tip_history = self.tip_history[-50:]
                
            self.settings["total_tips"] = self.total_tips
            self.settings["tip_history"] = self.tip_history
            self.save_settings()
            
            # Add to user tips ledger
            if sender.username not in self.user_tips_ledger:
                self.user_tips_ledger[sender.username] = 0
            self.user_tips_ledger[sender.username] += amount
            
            self.settings["total_tips"] = self.total_tips
            self.settings["tip_history"] = self.tip_history
            self.settings["user_tips_ledger"] = self.user_tips_ledger
            self.save_settings()
            
            await self.highrise.chat(f"💖 WOW! Thank you for the {amount} Gold, {sender.username}! Your VIP balance is now {self.user_tips_ledger[sender.username]}G. Type -buyvip to claim VIP! ✨")
            try:
                await self.highrise.send_emote("emote-pose7", sender.id)
            except: pass
            
            print(f"Received {amount} tips from {sender.username}. Ledger: {self.user_tips_ledger[sender.username]}")



    async def on_channel(self, user_id: str, channel_id: str, message: str):
        """
        Triggered when a user opens the bot's profile or sends a channel message.
        We'll use this to send a math question via DM.
        """
        try:
            # Get user info
            room_users = (await self.highrise.get_room_users()).content
            username = None
            for u, _ in room_users:
                if u.id == user_id:
                    username = u.username
                    break
            
            if not username:
                # User might have left, try to get from conversations
                conversations = await self.highrise.get_conversations()
                for conv in conversations.conversations:
                    if conv.id == channel_id:
                        # This is a bit tricky, we might not have username
                        # Let's just use a generic approach
                        username = "User"
                        break
            
            # Generate a random math question
            num1 = random.randint(1, 20)
            num2 = random.randint(1, 20)
            operations = ["+", "-", "×"]
            operation = random.choice(operations)
            
            if operation == "+":
                answer = num1 + num2
                question = f"{num1} + {num2}"
            elif operation == "-":
                # Make sure result is positive
                if num1 < num2:
                    num1, num2 = num2, num1
                answer = num1 - num2
                question = f"{num1} - {num2}"
            else:  # multiplication
                num1 = random.randint(1, 10)
                num2 = random.randint(1, 10)
                answer = num1 * num2
                question = f"{num1} × {num2}"
            
            # Send math question via DM
            math_message = (
                f"👋 Hello @{username}! Welcome to my profile! 🤖\n\n"
                f"🧮 **Quick Math Challenge:**\n"
                f"What is {question} = ?\n\n"
                f"💡 Reply with your answer!\n"
                f"✨ ادخل معانا في {self.room_name}: https://high.rs/room?id=694642f094977936f78a313f&invite_id=6958a4f4cdac317262837bcf"
            )
            
          # Get or create conversation
            try:
                # Try to send message to the channel
                await self.highrise.send_message(channel_id, math_message)
                print(f"Sent math question to {username}: {question} = {answer}")
            except Exception as e:
                print(f"Error sending math question: {e}")
                
        except Exception as e:
            print(f"on_channel error: {e}")
# --- MANAGER LOGIC (CONSOLIDATED) ---
LOCK_FILE = "manager.lock"
COOLDOWN_SECONDS = 30 

def cleanup_orphaned_processes():
    """Kills any existing highrise bot processes to prevent multi-login issues on startup."""
    print("[INIT] Cleaning up potentially orphaned bot processes...")
    try:
        import psutil
        current_pid = os.getpid()
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # Check if it's a python process running highrise bots, but not the manager itself
                cmd = proc.info.get('cmdline') or []
                if proc.info['pid'] != current_pid and "python" in proc.info['name'].lower():
                    if "highrise" in cmd and "main:MyBot" in cmd:
                        print(f"[CLEANUP] Killing orphaned bot process: {proc.info['pid']}")
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        # Fallback to taskkill if psutil is not available
        print("[WARN] psutil not installed. Attempting basic cleanup via taskkill...")
        os.system('taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq highrise*" >nul 2>&1')
        print("[WARN] Cleanup finished (limited precision without psutil).")

def acquire_lock():
    """Ensures only one instance of the manager is running."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = f.read().strip()
                if not old_pid:
                    raise ValueError("Empty lock file")
                old_pid = int(old_pid)
            
            # Check if process is actually running
            import psutil
            if psutil.pid_exists(old_pid):
                # Double check it's actually a python process
                proc = psutil.Process(old_pid)
                if "python" in proc.name().lower():
                    print(f"[ERROR] Another manager is already running (PID: {old_pid}). Exiting.")
                    sys.exit(1)
            
            os.remove(LOCK_FILE)
        except:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
    
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def release_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except: pass

def load_bots():
    bots = []
    seen_tokens = set()
    
    # helper to add bot with normalized token
    def add_bot(b_data):
        token = b_data.get("token")
        if token:
            token = token.strip()
            if token not in seen_tokens:
                b_data["token"] = token # update with stripped version
                bots.append(b_data)
                seen_tokens.add(token)
                return True
        return False

    # 1. Load bots from bots_config.json
    if os.path.exists("bots_config.json"):
        try:
            with open("bots_config.json", "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for b in data:
                        add_bot(b)
        except Exception as e:
            print(f"[ERROR] Error reading bots_config.json: {e}")

    # 2. Add bot from config.json if not already present (Backward Compatibility)
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                conf = json.load(f)
                room_id = conf.get("room_id")
                token = conf.get("bot_token") or conf.get("token")
                
                if room_id and token:
                    add_bot({
                        "name": "Main_Bot",
                        "room_id": room_id,
                        "token": token,
                        "status": "running",
                        "force_restart": conf.get("force_restart", False)
                    })
        except Exception as e:
            print(f"[WARN] Error reading config.json: {e}")
    return bots

def run_all_bots():
    # Set console to UTF-8 to prevent encoding errors on Windows
    try:
        if sys.stdout.encoding.lower() != 'utf-8':
            sys.stdout.reconfigure(encoding='utf-8')
    except: pass

    acquire_lock()
    cleanup_orphaned_processes()
    
    print("\n" + "="*40)
    print("      HIGHRISE MULTI-BOT MANAGER")
    print("="*40 + "\n")
    
    # Store process objects and their bot info: {pid: {"bot": bot_data, "process": p}}
    running_processes = {}
    
    # Track used tokens to prevent multi-login within THIS manager
    used_tokens = {} # {token: bot_name}
    
    # Add restart cooldown to prevent spamming restarts during multi-login issues
    # {token: last_stop_timestamp}
    restart_cooldowns = {}

    try:
        while True:
            # Refresh bots list from config
            bots = load_bots()
            active_bots = [b for b in bots if b.get("status") == "running"]
            
            # Check for dead processes
            dead_pids = []
            now = time.time()
            for pid, entry in running_processes.items():
                p = entry["process"]
                bot = entry["bot"]
                if p.poll() is not None:
                    exit_code = p.poll()
                    status_str = "normally" if exit_code == 0 else f"with error code {exit_code}"
                    print(f"[INFO] Bot '{bot.get('name')}' (PID: {pid}) has stopped {status_str}.")
                    dead_pids.append(pid)
                    token = bot.get("token")
                    if token in used_tokens:
                        del used_tokens[token]
                    # Set cooldown for this token
                    # If it stopped normally, use a much shorter cooldown (5s) for ASAP restart
                    restart_cooldowns[token] = now - (COOLDOWN_SECONDS - 5) if exit_code == 0 else now
            
            for pid in dead_pids:
                del running_processes[pid]

            # Track tokens we are TRYING to launch in this iteration to avoid starting 
            # multiple bots with the same token if they are both "active" in config
            tokens_to_skip = set()

            # Try to launch bots that aren't running
            for bot in active_bots:
                name = bot.get("name", "Unknown")
                token = bot.get("token")
                room_id = bot.get("room_id")
                
                if not room_id or not token:
                    continue
                
                # 1. Check if token is already active/running
                if token in used_tokens:
                    continue
                
                # 2. Check if we already decided to start a bot with this token this loop
                if token in tokens_to_skip:
                    continue
                
                # 3. Prevent duplicate tokens from even being considered for launch
                tokens_to_skip.add(token)

                # 4. Check for 'force_restart' (Room Movement) - Priority Join
                is_force = bot.get("force_restart", False)
                
                # Normal Cooldown Check if not forced
                if not is_force:
                    if token in restart_cooldowns:
                        last_stop = restart_cooldowns[token]
                        if now - last_stop < COOLDOWN_SECONDS:
                            # Still in cooldown, skip for now
                            continue
                        else:
                            # Cooldown expired
                            del restart_cooldowns[token]
                else:
                    # Forced restart: Still wait 3s for session to clear to avoid multi-login
                    if token in restart_cooldowns:
                        last_stop = restart_cooldowns[token]
                        if now - last_stop < 3:
                            continue

                # Double check: Is this specific bot already running?
                if any(entry["bot"].get("name") == name for entry in running_processes.values()):
                    continue

                print(f"[INFO] Launching {name} in Room: {room_id}...")
                
                # Clear force_restart flag ONLY after deciding to launch
                if is_force:
                    try:
                        # Clear in bots_config.json
                        if os.path.exists("bots_config.json"):
                            with open("bots_config.json", "r") as f:
                                b_data = json.load(f)
                            for b in b_data:
                                if b.get("token") == token: b["force_restart"] = False
                            with open("bots_config.json", "w") as f:
                                json.dump(b_data, f, indent=4)
                        
                        # Clear in config.json
                        if os.path.exists("config.json"):
                            with open("config.json", "r") as f:
                                c_data = json.load(f)
                            if c_data.get("force_restart"):
                                c_data["force_restart"] = False
                                with open("config.json", "w") as f:
                                    json.dump(c_data, f, indent=4)
                        
                        if token in restart_cooldowns: del restart_cooldowns[token]
                    except: pass

                cmd = [sys.executable, "-m", "highrise", "main:MyBot", room_id, token]
                
                # Pass bot name via environment variable to avoid Highrise CLI argument errors
                env = os.environ.copy()
                env["BOT_NAME"] = name

                try:
                    p = subprocess.Popen(cmd, env=env)
                    running_processes[p.pid] = {"bot": bot, "process": p}
                    used_tokens[token] = name
                    time.sleep(1) # Staggered start
                except Exception as e:
                    print(f"[ERROR] Failed to launch {name}: {e}")

            if not running_processes and not active_bots:
                print("[IDLE] No active bots to run. Waiting...")
                time.sleep(10)
            
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n" + "!"*40)
        print(" [STOP] MANAGER SHUTTING DOWN...")
        print("!"*40)
        for pid, entry in running_processes.items():
            print(f"[STOP] Killing {entry['bot'].get('name')} (PID: {pid})...")
            try:
                # Force kill on Windows to ensure session is cleared
                if os.name == 'nt':
                    subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    entry["process"].terminate()
            except: pass
        
        release_lock()
        print("[DONE] Cleanup complete. Shutdown successful.")
    finally:
        release_lock()

if __name__ == "__main__":
    run_all_bots()
